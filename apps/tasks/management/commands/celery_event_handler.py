import ast

from django.core.management.base import BaseCommand
from celery.events import EventReceiver
from kombu import Connection
from django.utils import timezone

from apps.tasks.models import JobProtocol
from evil_flowers_catalog.celery import app


class Command(BaseCommand):
    help = "Listen to Celery task events and store them in the database"

    def handle_event(self, event):
        if not event.get("type", "").startswith("task-"):
            return

        try:
            protocol = JobProtocol.objects.get(pk=event["uuid"])
        except JobProtocol.DoesNotExist:
            protocol = JobProtocol.objects.create(
                id=event["uuid"],
                parent_id=event.get("parent_id"),
                name=event["name"],
                job_args=ast.literal_eval(event["args"]) if event.get("args") else None,
                job_kwargs=ast.literal_eval(event["kwargs"]) if event.get("kwargs") else None,
                result=ast.literal_eval(event["result"]) if event.get("result") else None,
            )

        match event["type"]:
            case "task-received":
                protocol.status = JobProtocol.JobStatus.RECEIVED
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
        self.stdout.write(self.style.SUCCESS("Starting Celery event listener..."))
        self.start_event_listener()
