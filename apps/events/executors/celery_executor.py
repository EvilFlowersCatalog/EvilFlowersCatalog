from celery import signature, group
from django.db.models import Model
from .executor import Executor
from evil_flowers_catalog.celery import app


class CeleryExecurtor(Executor):
    def __init__(self):
        self.celery_app = app

    def send(self, event: str, payload: dict | str):
        if isinstance(payload, dict):
            if "args" not in payload:
                payload["args"] = []
            if "kwargs" not in payload:
                payload["kwargs"] = {}
            if "queue" not in payload:
                payload["queue"] = event
        else:
            payload = {
                "args": [],
                "kwargs": {"content": payload},
                "queue": event,
            }

        # A caller that wants to recognise the task later (e.g. in a worker's
        # result report) mints its id and passes it as `task_id`.
        self.celery_app.send_task(
            event,
            args=payload["args"],
            kwargs=payload["kwargs"],
            immutable=True,
            queue=payload["queue"],
            task_id=payload.get("task_id"),
        )
