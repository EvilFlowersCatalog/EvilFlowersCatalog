from celery import signature, group
from .executor import Executor
from evil_flowers_catalog.celery import app


class CeleryExecurtor(Executor):
    def __init__(self):
        self.celery_app = app

    def send(self, event: str, payload: dict):
        self.celery_app.send_task(
            event,
            args=payload["args"],
            kwargs=payload["kwargs"],
            immutable=True,
            queue=payload["queue"],
        )
