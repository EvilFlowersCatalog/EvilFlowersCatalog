"""The Celery implementation of the `Dispatcher` port."""

from __future__ import annotations

import contextvars

_current_run = contextvars.ContextVar("evilflowers_job_run_id", default=None)


def current_run_id() -> str | None:
    """The run id the facade is dispatching right now.

    A domain service that sends its own task (`ContentEncryptionService`) sends it under this id, so
    the run row the facade wrote and the Celery task share it.
    """
    return _current_run.get()


class CeleryDispatcher:
    def send(
        self,
        task: str,
        *,
        task_id: str,
        args: list | None = None,
        kwargs: dict | None = None,
        queue: str | None = None,
    ) -> None:
        from evil_flowers_catalog.celery import app

        app.send_task(task, args=args or [], kwargs=kwargs or {}, task_id=task_id, queue=queue)

    def revoke(self, task_id: str, *, terminate: bool = False) -> None:
        from evil_flowers_catalog.celery import app

        app.control.revoke(task_id, terminate=terminate)


class running_as:
    """Context manager: `current_run_id()` returns `run_id` inside."""

    def __init__(self, run_id: str):
        self._run_id = run_id
        self._token = None

    def __enter__(self):
        self._token = _current_run.set(self._run_id)
        return self

    def __exit__(self, *exc):
        _current_run.reset(self._token)
        return False


__all__ = ["CeleryDispatcher", "current_run_id", "running_as"]
