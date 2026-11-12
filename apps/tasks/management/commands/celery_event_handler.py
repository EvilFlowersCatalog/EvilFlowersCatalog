import ast
import logging
import math

from django import db
from django.db import transaction
from django.core.management.base import BaseCommand
from celery.events import EventReceiver
from kombu import Connection
from django.utils import timezone

from apps.tasks.models import JobProtocol
from evil_flowers_catalog import profiling, telemetry
from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)
_tracer = telemetry.get_tracer("apps.tasks.event_handler")


def _literal(raw: str, fallback):
    """A task's repr'd args/kwargs/result as JSON-storable data.

    `literal_eval` also yields sets, tuples and bytes, which a JSONField rejects: every such event
    failed its insert and the protocol was lost (243 `set is not JSON serializable` a day on dev).
    The field is Celery's truncated `saferepr`, not the value itself: a container nested too deep or
    too long is elided to `{...}` / `[...]` / `...`, which evaluates to `{Ellipsis}` and `Ellipsis`
    (#122). Those become the string `"..."`.
    """
    try:
        return _json_safe(ast.literal_eval(raw))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return fallback


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if value is Ellipsis:
        return "..."
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    # bytes, complex, NaN/inf (jsonb rejects them) and anything else literal_eval may produce.
    return repr(value)


class Command(BaseCommand):
    help = "Listen to Celery task events and store them in the database"

    def handle_event(self, event):
        if not event.get("type", "").startswith("task-"):
            return

        if "uuid" not in event:
            return

        # One span per event (IP-019), so its queries form one trace instead of
        # each being a root span. The event type is a closed set (task-*).
        # A bad event is logged and skipped (#108): an exception escaping here ends
        # `recv.capture()`, and Celery does not queue events for an absent listener,
        # so every event sent until supervisord restarts it would be lost too.
        try:
            with _tracer.start_as_current_span(f"celery.event {event['type']}"), transaction.atomic():
                self._record(event)
        except Exception as error:
            logger.exception(
                "Skipping Celery event: type=%s task_id=%s parent_id=%s",
                event.get("type"),
                event.get("uuid"),
                event.get("parent_id"),
            )
            if isinstance(error, db.DatabaseError):
                # A failed query may leave the connection unusable; the next event reconnects.
                db.close_old_connections()

    def _record(self, event):
        try:
            protocol = JobProtocol.objects.get(pk=event["uuid"])
        except JobProtocol.DoesNotExist:
            protocol = JobProtocol(
                id=event["uuid"],
                parent_id=event.get("parent_id"),
                name=event.get("name", "unknown"),
            )
            if event.get("args"):
                protocol.job_args = _literal(event["args"], [event["args"]])
            if event.get("kwargs"):
                protocol.job_kwargs = _literal(event["kwargs"], {"raw": event["kwargs"]})
            if event.get("result"):
                protocol.result = _literal(event["result"], {"raw": event["result"]})

        match event["type"]:
            case "task-received":
                protocol.status = JobProtocol.JobStatus.RECEIVED
                if event.get("name"):
                    protocol.name = event["name"]
            case "task-succeeded":
                protocol.status = JobProtocol.JobStatus.SUCCESS
                protocol.finished_at = timezone.now()
            case "task-started":
                protocol.status = JobProtocol.JobStatus.IN_PROGRESS
                protocol.worker = event.get("hostname")
                protocol.started_at = timezone.now()
            case "task-failed":
                protocol.status = JobProtocol.JobStatus.FAILED
                protocol.finished_at = timezone.now()

        protocol.save()

    def start_event_listener(self):
        with Connection(app.conf.broker_url) as connection:
            recv = EventReceiver(
                connection,
                handlers={
                    "*": self.handle_event,
                },
            )
            recv.capture(limit=None, timeout=None, wakeup=True)

    def handle(self, *args, **options):
        # IP-019: this process is started by supervisord and never forks.
        # SIGTERM becomes SystemExit so `atexit` flushes the exporters.
        profiling.setup("event-handler")
        telemetry.setup("event-handler")
        telemetry.exit_on_sigterm()

        logger.info("Starting Celery event listener")
        self.start_event_listener()
