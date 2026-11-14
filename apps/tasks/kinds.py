"""Job kinds (IP-031): what one kind of background work needs.

A kind names a Celery task and says what it is about (its subject), who may start it, how to start
it, how a worker's report changes the subject, and what else an operator can do with it (actions).
Kinds register at app start from each app's `jobs.py`; `registry` is the lookup by name and by task.

Most kinds are small: a subject model, a task name and how to read the subject back out of a call
(`subject_from_call`). Kinds whose dispatch already lives in a domain service (LCP encryption,
extraction) override `dispatch` and call that service; the service sends the task under the id
`JobService` pre-assigned (`preassigned_task_id`), so the ledger row exists before any worker can
see the task.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple, Type

from django.apps import apps as django_apps
from django.db import models

if TYPE_CHECKING:
    from apps.tasks.models import JobProtocol

logger = logging.getLogger(__name__)

_preassigned = contextvars.ContextVar("evilflowers_job_task_id", default=None)


def preassigned_task_id() -> Optional[str]:
    """The task id `JobService` reserved for the dispatch running now, if any.

    A domain service that mints its own task id (`ContentEncryptionService._record_dispatch`) uses
    this one instead, so the task it sends and the ledger row `JobService` wrote share the id.
    """
    return _preassigned.get()


class JobRefused(Exception):
    """A start, retry, action or report the kind or the job's state does not allow.

    `status` is the HTTP status the API answers with (409 conflict, 422 not eligible).
    """

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class JobAction:
    """A kind-specific operation on an existing job, e.g. LCP `requeue` or `reencrypt`."""

    name: str
    title: str
    description: str
    handler: Callable[["JobProtocol", Any, dict, Any], dict]
    options_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    #: Reads only (a dry run): no audit log line, allowed while the job is active.
    read_only: bool = False


class JobKind:
    """Base class of a job kind. Subclasses set the class attributes and override what differs."""

    #: Registry name, `<area>.<work>`.
    name: str = ""
    title: str = ""
    description: str = ""
    #: Celery task name this kind sends and recognises.
    task: str = ""
    #: None: the router (`CELERY_TASK_ROUTES`) decides.
    queue: Optional[str] = None
    #: None for kinds about nothing in particular (beat maintenance).
    subject_model: Optional[Type[models.Model]] = None
    #: Who may start, retry, revoke and run actions. `core.change_entry` is a superuser check in
    #: practice (`Entry` has no default permissions): the administrator gate of #148.
    permission: str = "core.change_entry"
    #: One active job per subject; a second start answers 409 unless `force`.
    exclusive: bool = True
    #: Startable through the API and MCP (some kinds only make sense as a reaction to a domain event).
    startable: bool = True
    #: The kind owns a deterministic task id (`task_id_for`): a retry reuses the row (Q9).
    stable_task_id: bool = False
    #: Sweeper limit for a started job without events; None: the catalog's or the service default.
    time_limit: Optional[timedelta] = None
    #: `revoke(terminate=True)` may kill a running task (Q7). Off for workers that write files.
    terminate_safe: bool = False
    #: The worker reports its outcome to `POST /api/v1/jobs/{id}/report`.
    reports: bool = False
    #: JSON Schema of start options.
    options_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}
    actions: Dict[str, JobAction] = {}

    # -- subject ------------------------------------------------------------------------------------

    def get_subject(self, subject_id) -> Optional[models.Model]:
        if self.subject_model is None or subject_id is None:
            return None
        try:
            return self.subject_model._default_manager.get(pk=subject_id)
        except (self.subject_model.DoesNotExist, ValueError, TypeError):
            return None

    def subject_from_call(self, args: list, kwargs: dict) -> Optional[str]:
        """The subject's primary key, read from the task's arguments; None when it cannot be told.

        Lets the ledger attribute a task to its subject however it was sent (`.delay()` in a signal,
        beat, a service's follow-up), not only when `JobService` started it.
        """
        return None

    def scope(self, subject) -> Tuple[Optional[Any], Optional[Any]]:
        """(catalog_id, entry_id) of the subject, for filtering and access."""
        return None, None

    # -- starting -----------------------------------------------------------------------------------

    def eligible(self, subject, options: dict) -> Optional[str]:
        """None when the kind can run on `subject`, otherwise the reason it cannot (422)."""
        return None

    def task_id_for(self, subject, options: dict) -> Optional[str]:
        """The task id for `stable_task_id` kinds; None mints a fresh one."""
        return None

    def args(self, job: "JobProtocol", subject, options: dict) -> list:
        return []

    def kwargs(self, job: "JobProtocol", subject, options: dict) -> dict:
        return {}

    def dispatch(self, job: "JobProtocol", subject, options: dict) -> None:
        """Send the task under `job.pk`. Raise `JobRefused` when nothing could be sent."""
        from evil_flowers_catalog.celery import app

        app.send_task(
            self.task,
            args=self.args(job, subject, options),
            kwargs=self.kwargs(job, subject, options),
            task_id=str(job.pk),
            queue=self.queue,
        )

    def retry_options(self, job: "JobProtocol") -> dict:
        """Options for a retry of `job`; by default the ones it was started with."""
        return dict(job.options or {})

    # -- results ------------------------------------------------------------------------------------

    def on_report(self, job: "JobProtocol", subject, payload: dict) -> dict:
        """Apply a worker's report to the subject; return what to store as `job.report`."""
        raise JobRefused("This kind of job does not take reports.", status=400)

    def detail(self, job: "JobProtocol", subject) -> dict:
        """Kind-specific fields of the job response (e.g. the LCP job of #148)."""
        return {}

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "task": self.task,
            "subject_type": _label(self.subject_model),
            "startable": self.startable,
            "exclusive": self.exclusive,
            "reports": self.reports,
            "terminate_safe": self.terminate_safe,
            "options_schema": self.options_schema,
            "actions": [
                {
                    "name": action.name,
                    "title": action.title,
                    "description": action.description,
                    "options_schema": action.options_schema,
                    "read_only": action.read_only,
                }
                for action in self.actions.values()
            ],
        }


class SystemKind(JobKind):
    """A beat task about nothing in particular: startable by hand, one at a time."""

    def kwargs(self, job, subject, options):
        return {}


def _label(model) -> Optional[str]:
    return model._meta.label_lower if model is not None else None


def entry_scope(entry) -> Tuple[Optional[Any], Optional[Any]]:
    return (entry.catalog_id, entry.pk) if entry is not None else (None, None)


def first_arg(args: list, kwargs: dict, name: str) -> Optional[str]:
    """The subject id passed as the first positional argument or as `name`."""
    value = kwargs.get(name) if isinstance(kwargs, dict) else None
    if value is None and isinstance(args, (list, tuple)) and args:
        value = args[0]
    return _uuid_or_none(value)


def _uuid_or_none(value) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


class Registry:
    def __init__(self):
        self._by_name: Dict[str, JobKind] = {}
        self._by_task: Dict[str, JobKind] = {}
        self._loaded = False

    def register(self, kind_class: Type[JobKind]) -> Type[JobKind]:
        kind = kind_class()
        if not kind.name or not kind.task:
            raise ValueError(f"{kind_class.__name__} needs `name` and `task`")
        if kind.name in self._by_name:
            raise ValueError(f"job kind {kind.name} registered twice")
        self._by_name[kind.name] = kind
        # One task can serve one kind; the first registration wins and a second is a bug.
        if kind.task in self._by_task:
            raise ValueError(f"task {kind.task} already belongs to {self._by_task[kind.task].name}")
        self._by_task[kind.task] = kind
        return kind_class

    def load(self) -> None:
        """Import every installed app's `jobs` module once (called from `TasksConfig.ready`)."""
        if self._loaded:
            return
        self._loaded = True
        for config in django_apps.get_app_configs():
            try:
                import_module(f"{config.name}.jobs")
            except ModuleNotFoundError as error:
                if error.name != f"{config.name}.jobs":
                    raise

    def get(self, name: Optional[str]) -> Optional[JobKind]:
        self.load()
        return self._by_name.get(name) if name else None

    def for_task(self, task_name: Optional[str]) -> Optional[JobKind]:
        self.load()
        return self._by_task.get(task_name) if task_name else None

    def all(self):
        self.load()
        return sorted(self._by_name.values(), key=lambda kind: kind.name)

    def names(self):
        return [kind.name for kind in self.all()]


registry = Registry()

__all__ = [
    "JobAction",
    "JobKind",
    "JobRefused",
    "SystemKind",
    "entry_scope",
    "first_arg",
    "preassigned_task_id",
    "registry",
]
