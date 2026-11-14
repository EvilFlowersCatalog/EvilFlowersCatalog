"""Writing the job ledger (IP-031): what was sent, and what the Celery events say happened to it.

Two writers share one rule set:

- `record_publish` runs in the process that sends a task (`before_task_publish`), so a task the
  catalog sends is in the ledger, queued, before any worker can take it, with its exact arguments.
- `apply_event` runs in `manage.py celery_event_handler` for every `task-*` event of every worker,
  the catalog's and the services'.

Precedence (one rule): an event older than the row's `last_event_at` is history, not state;
`revoked` is final; any other finished status reopens only on a newer `task-received` or
`task-started` (a redelivery, a Celery retry, a re-queue of a stable-id kind).
"""

from __future__ import annotations

import ast
import logging
import math
import re
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Optional, Tuple

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.tasks.kinds import registry
from apps.tasks.models import JobEvent, JobProtocol

logger = logging.getLogger(__name__)

Status = JobProtocol.JobStatus

STATUS_BY_EVENT = {
    "task-sent": Status.QUEUED,
    "task-received": Status.RECEIVED,
    "task-started": Status.STARTED,
    "task-retried": Status.RETRYING,
    "task-succeeded": Status.SUCCEEDED,
    "task-failed": Status.FAILED,
    "task-revoked": Status.REVOKED,
    "task-rejected": Status.REJECTED,
}

#: Events worth keeping for a job without a kind (the beat noise succeeds every minute; Q6).
_ALWAYS_LOGGED = {"task-failed", "task-retried", "task-revoked", "task-rejected"}

#: Keys whose values never reach the ledger (Dataverse passes its API token as a task argument).
SECRET_KEY = re.compile(r"(token|password|passwd|secret|api_?key|authorization|credential)", re.IGNORECASE)
REDACTED = "[redacted]"

_TEXT_LIMIT = 20_000


# -- values ---------------------------------------------------------------------------------------


def literal(raw, fallback):
    """A task's repr'd args/kwargs/result (from an event) as JSON-storable data.

    `literal_eval` also yields sets, tuples and bytes, which a JSONField rejects. The field is
    Celery's truncated `saferepr`, not the value: a container nested too deep or too long is elided
    to `{...}` / `[...]` / `...`, which evaluates to `{Ellipsis}` and `Ellipsis` (#122); those become
    the string `"..."`.
    """
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
    # bytes, complex, NaN/inf (jsonb rejects them) and anything else literal_eval may produce.
    return repr(value)


def redact(value) -> Tuple[object, bool]:
    """`value` with every secret-looking key's value replaced; and whether anything was."""
    if isinstance(value, dict):
        redacted = False
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and SECRET_KEY.search(key):
                result[key] = REDACTED
                redacted = True
            else:
                result[key], inner = redact(item)
                redacted = redacted or inner
        return result, redacted
    if isinstance(value, list):
        items = [redact(item) for item in value]
        return [item for item, _ in items], any(flag for _, flag in items)
    return value, False


def _text(value) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"


def _timestamp(event) -> datetime:
    raw = event.get("timestamp")
    try:
        return datetime.fromtimestamp(float(raw), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return timezone.now()


def ignored(task_name: Optional[str]) -> bool:
    return bool(task_name) and task_name in settings.EVILFLOWERS_JOBS_IGNORE_TASKS


# -- attribution ------------------------------------------------------------------------------------


def attribute(job: JobProtocol) -> None:
    """Fill kind, subject and scope from the task name and arguments, or from the parent job.

    Changes `job` in memory only. A kind is set once; a subject only when none is known.
    """
    kind = registry.for_task(job.name) if job.name else None
    if kind is not None and not job.kind:
        job.kind = kind.name
    if job.subject_id is None and kind is not None and kind.subject_model is not None:
        subject_id = kind.subject_from_call(job.job_args or [], job.job_kwargs or {})
        subject = kind.get_subject(subject_id) if subject_id else None
        if subject is not None:
            set_subject(job, kind, subject)
    if job.subject_id is None and job.parent_id:
        # A service's follow-up task (the extraction workers' jobs) belongs to its parent's subject.
        parent = (
            JobProtocol.objects.filter(pk=job.parent_id)
            .only("subject_type_id", "subject_id", "catalog_id", "entry_id")
            .first()
        )
        if parent is not None and parent.subject_id is not None:
            job.subject_type_id = parent.subject_type_id
            job.subject_id = parent.subject_id
            job.catalog_id = parent.catalog_id
            job.entry_id = parent.entry_id


def set_subject(job: JobProtocol, kind, subject) -> None:
    job.subject_type = ContentType.objects.get_for_model(subject, for_concrete_model=True)
    job.subject_id = subject.pk
    job.catalog_id, job.entry_id = kind.scope(subject)


# -- publish ----------------------------------------------------------------------------------------


def record_publish(headers: dict, body, routing_key: Optional[str] = None) -> None:
    """Write (or complete) the ledger row of a task the current process is sending.

    Called from `before_task_publish` with Celery's message protocol 2: `headers` carry `id`,
    `task`, `parent_id`; `body` is `(args, kwargs, embed)`. Never raises: a ledger failure must not
    stop a task from being sent.
    """
    task_id = headers.get("id")
    name = headers.get("task")
    if not task_id or ignored(name):
        return
    try:
        uuid.UUID(str(task_id))
    except ValueError:
        # Every ledger id is a UUID (#108); a task sent under another id is not recorded.
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
            job = JobProtocol.objects.select_for_update().filter(pk=task_id).first()
            if job is None:
                job = JobProtocol(
                    id=task_id,
                    name=name,
                    parent_id=headers.get("parent_id"),
                    queue=routing_key,
                    status=Status.QUEUED,
                    job_args=args,
                    job_kwargs=kwargs,
                    args_exact=exact,
                    sent_at=now,
                )
                attribute(job)
                try:
                    with transaction.atomic():
                        job.save(force_insert=True)
                    return
                except IntegrityError:
                    # The listener saw a `task-sent` from another publisher of the same id first.
                    job = JobProtocol.objects.select_for_update().get(pk=task_id)

            # JobService wrote the row first, or a Celery retry re-sends the same id (the task is still
            # running then), or a stable-id kind sends the id again (extraction re-queued on upload).
            if job.status in JobProtocol.TERMINAL_STATUSES:
                job.status = Status.QUEUED
                job.sent_at = None
                job.received_at = job.started_at = job.finished_at = None
                job.result = job.error = job.traceback = job.report = None
                job.reported_at = job.revoke_requested_at = None
                job.last_event_at = now
                job.args_exact = False
            job.sent_at = job.sent_at or now
            job.queue = job.queue or routing_key
            if job.name in ("", "unknown"):
                job.name = name
            if not job.args_exact:
                job.job_args, job.job_kwargs, job.args_exact = args, kwargs, exact
            attribute(job)
            job.save()
    except Exception:
        logger.exception("jobs.publish_not_recorded task_id=%s task=%s", task_id, name)


# -- events -----------------------------------------------------------------------------------------


def _get_or_create(task_id: str, event: dict) -> Tuple[JobProtocol, bool]:
    try:
        return JobProtocol.objects.select_for_update().get(pk=task_id), False
    except JobProtocol.DoesNotExist:
        pass
    job = JobProtocol(id=task_id, name=event.get("name") or "unknown", parent_id=event.get("parent_id"))
    try:
        with transaction.atomic():
            job.save(force_insert=True)
        return job, True
    except IntegrityError:
        # `record_publish` or `JobService` inserted it between the read and the insert.
        return JobProtocol.objects.select_for_update().get(pk=task_id), False


def _may_change(current: str, new: str) -> bool:
    if current == Status.REVOKED:
        return new == Status.REVOKED
    if new == Status.QUEUED:
        return current == Status.QUEUED
    if current in JobProtocol.TERMINAL_STATUSES:
        return new in (Status.RECEIVED, Status.STARTED) or new == current
    if new == Status.RETRYING:
        return current in (Status.RECEIVED, Status.STARTED, Status.RETRYING)
    return True


def apply_event(event: dict) -> Optional[JobProtocol]:
    """Apply one Celery `task-*` event to the ledger; the job it touched, or None when skipped.

    Runs inside the caller's transaction.
    """
    event_type = event.get("type", "")
    task_id = event.get("uuid")
    new_status = STATUS_BY_EVENT.get(event_type)
    if new_status is None or not task_id:
        return None
    if ignored(event.get("name")):
        return None

    timestamp = _timestamp(event)
    job, created = _get_or_create(task_id, event)
    if ignored(job.name):
        return None

    # Data the event carries, whatever the state rule decides.
    if event.get("name") and job.name in ("", "unknown"):
        job.name = event["name"]
    if event.get("parent_id") and not job.parent_id:
        job.parent_id = event["parent_id"]
    if not job.args_exact:
        if event.get("args"):
            job.job_args, _ = redact(literal(event["args"], [event["args"]]))
        if event.get("kwargs"):
            job.job_kwargs, _ = redact(literal(event["kwargs"], {"raw": event["kwargs"]}))
    if event.get("queue") or event.get("routing_key"):
        job.queue = job.queue or event.get("queue") or event.get("routing_key")

    current_state = job.last_event_at is None or timestamp >= job.last_event_at
    applies = current_state and _may_change(job.status, new_status)

    if applies:
        reopening = job.status in JobProtocol.TERMINAL_STATUSES and new_status in (Status.RECEIVED, Status.STARTED)
        if reopening:
            job.finished_at = None
            job.error = None
            job.traceback = None
        job.status = new_status
        job.last_event_at = timestamp

        if event_type == "task-sent":
            job.sent_at = job.sent_at or timestamp
        elif event_type == "task-received":
            job.received_at = timestamp
            job.worker = _text(event.get("hostname")) or job.worker
        elif event_type == "task-started":
            job.started_at = timestamp
            job.worker = _text(event.get("hostname")) or job.worker
            job.attempts = (job.attempts or 0) + 1
        elif event_type == "task-retried":
            job.error = _text(event.get("exception"))
            job.traceback = _text(event.get("traceback"))
        elif event_type == "task-succeeded":
            job.finished_at = timestamp
            if event.get("result") not in (None, "", "None"):
                job.result, _ = redact(literal(event["result"], {"raw": event["result"]}))
            if event.get("runtime") is not None:
                job.runtime = float(event["runtime"])
        elif event_type in ("task-failed", "task-rejected"):
            job.finished_at = timestamp
            job.error = _text(event.get("exception")) or job.error
            job.traceback = _text(event.get("traceback")) or job.traceback
        elif event_type == "task-revoked":
            job.finished_at = job.finished_at or timestamp
            reason = "terminated" if event.get("terminated") else "expired" if event.get("expired") else "revoked"
            job.error = job.error or reason

    if job.kind is None or job.subject_id is None:
        attribute(job)

    job.save()

    # History for jobs about something or started by someone; beat runs (every minute) only on trouble (Q6).
    if job.subject_id or job.requested_by_id or event_type in _ALWAYS_LOGGED:
        JobEvent.objects.create(
            job=job,
            type=event_type,
            timestamp=timestamp,
            hostname=_text(event.get("hostname")),
            data=_event_data(event, applied=applies),
        )
    return job


def _event_data(event: dict, *, applied: bool) -> dict:
    data = {"applied": applied}
    for key in ("exception", "traceback", "runtime", "retries", "terminated", "signum", "expired", "queue"):
        if event.get(key) not in (None, ""):
            value = event[key]
            data[key] = _text(value) if isinstance(value, str) else json_safe(value)
    return data


def log(job: JobProtocol, event_type: str, **data) -> None:
    """A catalog-side event (queued by JobService, revoke requested, lost, reported)."""
    JobEvent.objects.create(job=job, type=event_type, timestamp=timezone.now(), data=json_safe(data))


__all__ = [
    "REDACTED",
    "STATUS_BY_EVENT",
    "apply_event",
    "attribute",
    "ignored",
    "json_safe",
    "literal",
    "log",
    "record_publish",
    "redact",
    "set_subject",
]
