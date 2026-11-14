"""The run ledger (IP-031): facts about Celery task executions, handed to the facade.

- `record_publish` runs in the process that sends a task (`before_task_publish`): the run is in the
  ledger, queued, with its exact arguments, before any worker can take it.
- `apply_event` runs in `manage.py celery_event_handler` for every `task-*` event of every worker.

Both apply one precedence rule (`apps.tasks.domain.machine.run_accepts`) and then tell the facade,
which moves the run's job through the job state machine.
"""

from __future__ import annotations

import ast
import logging
import math
import re
import uuid
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.tasks.application.registry import registry
from apps.tasks.domain import machine
from apps.tasks.domain.values import RunStatus
from apps.tasks.models import JobEvent, JobRun

logger = logging.getLogger(__name__)

STATUS_BY_EVENT = {
    "task-sent": RunStatus.QUEUED,
    "task-received": RunStatus.RECEIVED,
    "task-started": RunStatus.STARTED,
    "task-retried": RunStatus.RETRYING,
    "task-succeeded": RunStatus.SUCCEEDED,
    "task-failed": RunStatus.FAILED,
    "task-revoked": RunStatus.REVOKED,
    "task-rejected": RunStatus.REJECTED,
}

#: The reconciler's own tick is not a job: recording it would be a row per minute about nothing.
ALWAYS_IGNORED = frozenset({"apps.tasks.tasks.reconcile_jobs"})

#: Events worth keeping for a run without a job (beat noise succeeds every minute; Q6).
_ALWAYS_LOGGED = {"task-failed", "task-retried", "task-revoked", "task-rejected"}

#: Keys whose values never reach the ledger (Dataverse passes its API token as a task argument).
SECRET_KEY = re.compile(r"(token|password|passwd|secret|api_?key|authorization|credential)", re.IGNORECASE)
REDACTED = "[redacted]"

_TEXT_LIMIT = 20_000


# -- values ---------------------------------------------------------------------------------------


def literal(raw, fallback):
    """A task's repr'd args/kwargs/result (from an event) as JSON-storable data (#122: `...` → "...")."""
    if not isinstance(raw, str):
        return json_safe(raw)
    try:
        return json_safe(ast.literal_eval(raw))
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return fallback


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [json_safe(item) for item in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if value is Ellipsis:
        return "..."
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    return repr(value)


def redact(value) -> tuple[object, bool]:
    """`value` with every secret-looking key's value replaced; and whether anything was."""
    match value:
        case dict():
            redacted, result = False, {}
            for key, item in value.items():
                if isinstance(key, str) and SECRET_KEY.search(key):
                    result[key], redacted = REDACTED, True
                else:
                    result[key], inner = redact(item)
                    redacted = redacted or inner
            return result, redacted
        case list():
            items = [redact(item) for item in value]
            return [item for item, _ in items], any(flag for _, flag in items)
        case _:
            return value, False


def _text(value) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"


def _timestamp(event) -> datetime:
    try:
        return datetime.fromtimestamp(float(event.get("timestamp")), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return timezone.now()


def ignored(task_name: str | None) -> bool:
    return bool(task_name) and (task_name in ALWAYS_IGNORED or task_name in settings.EVILFLOWERS_JOBS_IGNORE_TASKS)


def _facade():
    from apps.tasks.application.facade import facade

    return facade


# -- attribution ----------------------------------------------------------------------------------


def attribute(run: JobRun) -> None:
    """Kind, subject and scope of a run from its task name and arguments, or from its parent run."""
    definition = registry.for_task(run.name) if run.name else None
    if definition is not None and not run.kind:
        run.kind = definition.kind
    if run.subject_id is None and definition is not None and definition.subject_model is not None:
        subject_id = definition.subject_from_call(run.job_args or [], run.job_kwargs or {})
        subject = _load(definition.subject_model, subject_id) if subject_id else None
        if subject is not None:
            run.subject_type = ContentType.objects.get_for_model(subject, for_concrete_model=True)
            run.subject_id = subject.pk
            scope = definition.scope(subject)
            run.catalog_id, run.entry_id = scope.catalog_id, scope.entry_id
    if run.subject_id is None and run.parent_id:
        parent = (
            JobRun.objects.filter(pk=run.parent_id)
            .only("subject_type_id", "subject_id", "catalog_id", "entry_id", "job_id")
            .first()
        )
        if parent is not None:
            run.subject_type_id, run.subject_id = parent.subject_type_id, parent.subject_id
            run.catalog_id, run.entry_id = parent.catalog_id, parent.entry_id
            run.job_id = run.job_id or parent.job_id


def _load(model, pk):
    try:
        return model._default_manager.filter(pk=pk).first()
    except (ValueError, TypeError):
        return None


# -- publish --------------------------------------------------------------------------------------


def record_publish(headers: dict, body, routing_key: str | None = None) -> None:
    """Write (or complete) the run of a task the current process is sending. Never raises."""
    task_id = headers.get("id")
    name = headers.get("task")
    if not task_id or ignored(name):
        return
    try:
        uuid.UUID(str(task_id))
    except ValueError:
        logger.warning("jobs.publish_not_recorded task_id=%s task=%s reason=not-a-uuid", task_id, name)
        return
    try:
        args, kwargs = [], {}
        if isinstance(body, (list, tuple)) and len(body) >= 2:
            args, kwargs = body[0] or [], body[1] or {}
        args, args_redacted = redact(json_safe(list(args)))
        kwargs, kwargs_redacted = redact(json_safe(dict(kwargs)))
        exact = not (args_redacted or kwargs_redacted)
        now = timezone.now()

        with transaction.atomic():
            run = JobRun.objects.select_for_update().filter(pk=task_id).first()
            if run is None:
                run = JobRun(
                    id=task_id,
                    name=name,
                    parent_id=headers.get("parent_id"),
                    queue=routing_key,
                    status=RunStatus.QUEUED,
                    job_args=args,
                    job_kwargs=kwargs,
                    args_exact=exact,
                    sent_at=now,
                )
                attribute(run)
                try:
                    with transaction.atomic():
                        run.save(force_insert=True)
                except IntegrityError:
                    run = JobRun.objects.select_for_update().get(pk=task_id)
                else:
                    transaction.on_commit(lambda: _facade().run_published(run))
                    return

            # The facade wrote the run first, or a Celery retry re-sends the same id, or a stable-id kind
            # sends the id again (extraction re-queued on upload: the run starts over).
            if RunStatus(run.status).final:
                run.status = RunStatus.QUEUED
                run.received_at = run.started_at = run.finished_at = None
                run.result = run.error = run.traceback = run.report = None
                run.reported_at = None
                run.last_event_at = now
                run.args_exact = False
            run.sent_at = run.sent_at or now
            run.queue = run.queue or routing_key
            if run.name in ("", "unknown"):
                run.name = name
            if not run.args_exact:
                run.job_args, run.job_kwargs, run.args_exact = args, kwargs, exact
            attribute(run)
            run.save()
            transaction.on_commit(lambda: _facade().run_published(run))
    except Exception:
        logger.exception("jobs.publish_not_recorded task_id=%s task=%s", task_id, name)


# -- events ---------------------------------------------------------------------------------------


def _get_or_create(task_id: str, event: dict) -> JobRun:
    run = JobRun.objects.select_for_update().filter(pk=task_id).first()
    if run is not None:
        return run
    run = JobRun(id=task_id, name=event.get("name") or "unknown", parent_id=event.get("parent_id"))
    try:
        with transaction.atomic():
            run.save(force_insert=True)
        return run
    except IntegrityError:
        return JobRun.objects.select_for_update().get(pk=task_id)


def apply_event(event: dict) -> JobRun | None:
    """Apply one Celery `task-*` event; the run it touched, or None when skipped. Caller's transaction."""
    event_type = event.get("type", "")
    task_id = event.get("uuid")
    new_status = STATUS_BY_EVENT.get(event_type)
    if new_status is None or not task_id or ignored(event.get("name")):
        return None

    timestamp = _timestamp(event)
    run = _get_or_create(task_id, event)
    if ignored(run.name):
        return None

    if event.get("name") and run.name in ("", "unknown"):
        run.name = event["name"]
    if event.get("parent_id") and not run.parent_id:
        run.parent_id = event["parent_id"]
    if not run.args_exact:
        if event.get("args"):
            run.job_args, _ = redact(literal(event["args"], [event["args"]]))
        if event.get("kwargs"):
            run.job_kwargs, _ = redact(literal(event["kwargs"], {"raw": event["kwargs"]}))
    if event.get("queue") or event.get("routing_key"):
        run.queue = run.queue or event.get("queue") or event.get("routing_key")

    current = RunStatus(run.status) if run.last_event_at or run.status != RunStatus.QUEUED else None
    newer = run.last_event_at is None or timestamp >= run.last_event_at
    applies = machine.run_accepts(current, new_status, newer=newer)

    if applies:
        if current is not None and current.final and not new_status.final:
            run.finished_at = run.error = run.traceback = None
        run.status = new_status
        run.last_event_at = timestamp
        match event_type:
            case "task-sent":
                run.sent_at = run.sent_at or timestamp
            case "task-received":
                run.received_at = timestamp
                run.worker = _text(event.get("hostname")) or run.worker
            case "task-started":
                run.started_at = timestamp
                run.worker = _text(event.get("hostname")) or run.worker
                run.attempts = (run.attempts or 0) + 1
            case "task-retried":
                run.error = _text(event.get("exception"))
                run.traceback = _text(event.get("traceback"))
            case "task-succeeded":
                run.finished_at = timestamp
                if event.get("result") not in (None, "", "None"):
                    run.result, _ = redact(literal(event["result"], {"raw": event["result"]}))
                if event.get("runtime") is not None:
                    run.runtime = float(event["runtime"])
            case "task-failed" | "task-rejected":
                run.finished_at = timestamp
                run.error = _text(event.get("exception")) or run.error
                run.traceback = _text(event.get("traceback")) or run.traceback
            case "task-revoked":
                run.finished_at = run.finished_at or timestamp
                reason = "terminated" if event.get("terminated") else "expired" if event.get("expired") else "revoked"
                run.error = run.error or reason

    if run.kind is None or run.subject_id is None:
        attribute(run)
    run.save()

    if run.job_id or run.subject_id or event_type in _ALWAYS_LOGGED:
        JobEvent.objects.create(
            run=run,
            type=event_type,
            timestamp=timestamp,
            hostname=_text(event.get("hostname")),
            data=_event_data(event, applied=applies),
        )
    if applies:
        transaction.on_commit(lambda: _facade().run_changed(run))
    return run


def _event_data(event: dict, *, applied: bool) -> dict:
    data = {"applied": applied}
    for key in ("exception", "traceback", "runtime", "retries", "terminated", "signum", "expired", "queue"):
        if event.get(key) not in (None, ""):
            value = event[key]
            data[key] = _text(value) if isinstance(value, str) else json_safe(value)
    return data


__all__ = [
    "REDACTED",
    "STATUS_BY_EVENT",
    "apply_event",
    "attribute",
    "ignored",
    "json_safe",
    "literal",
    "record_publish",
    "redact",
]
