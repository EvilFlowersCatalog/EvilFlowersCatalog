import ast
import logging

from django.core.management.base import BaseCommand
from celery.events import EventReceiver
from kombu import Connection
from django.utils import timezone

from apps.tasks.models import JobProtocol
from evil_flowers_catalog import telemetry
from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)
_tracer = telemetry.get_tracer("apps.tasks.event_handler")


class Command(BaseCommand):
    help = "Listen to Celery task events and store them in the database"

    def handle_event(self, event):
        if not event.get("type", "").startswith("task-"):
            return

        if "uuid" not in event:
            return

        # One span per event (IP-019), so its queries form one trace instead of
        # each being a root span. The event type is a closed set (task-*).
        with _tracer.start_as_current_span(f"celery.event {event['type']}"):
            self._record(event)

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
                try:
                    protocol.job_args = ast.literal_eval(event["args"])
                except (ValueError, SyntaxError):
                    protocol.job_args = [event["args"]]
            if event.get("kwargs"):
                try:
                    protocol.job_kwargs = ast.literal_eval(event["kwargs"])
                except (ValueError, SyntaxError):
                    protocol.job_kwargs = {"raw": event["kwargs"]}
            if event.get("result"):
                try:
                    protocol.result = ast.literal_eval(event["result"])
                except (ValueError, SyntaxError):
                    protocol.result = {"raw": event["result"]}

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
        telemetry.setup("event-handler")
        telemetry.exit_on_sigterm()

        logger.info("Starting Celery event listener")
        self.start_event_listener()
