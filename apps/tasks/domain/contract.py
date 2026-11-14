"""The contract an app implements to contribute a kind of job (IP-031 rev. 2).

An app never adds job endpoints. It subclasses `JobDefinition` in `apps/<app>/jobs.py`, registers it,
and the core (facade, reconciler, `/api/v1/jobs`, MCP) does the rest: one state machine for every
kind. Everything here is plain Python; subjects are whatever the app's models are.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from apps.tasks.domain.values import Check, Observation, Ready, Scope


class Spec(BaseModel):
    """Base of every job spec: what a client may declare. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptySpec(Spec):
    pass


class Dispatcher(Protocol):
    """Port: sends Celery tasks (implemented in `apps.tasks.infrastructure.dispatcher`)."""

    def send(
        self,
        task: str,
        *,
        task_id: str,
        args: list | None = None,
        kwargs: dict | None = None,
        queue: str | None = None,
    ) -> None: ...

    def revoke(self, task_id: str, *, terminate: bool = False) -> None: ...


@dataclass(frozen=True, slots=True)
class Dispatch[S, P: Spec]:
    """Everything a definition needs to send one run."""

    job_id: str
    run_id: str
    subject: S
    spec: P
    dispatcher: Dispatcher

    def send(
        self, task: str, *, args: list | None = None, kwargs: dict | None = None, queue: str | None = None
    ) -> None:
        self.dispatcher.send(task, task_id=self.run_id, args=args, kwargs=kwargs, queue=queue)


class Declarer(Protocol):
    """Port: lets a definition declare follow-up jobs (the facade)."""

    def apply(
        self,
        kind: str,
        subject_id: Any = None,
        *,
        spec: Mapping | None = None,
        cause_id: Any = None,
        check_permission: bool = False,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class Report[S]:
    """A worker's report on one run."""

    job_id: str
    run_id: str
    subject: S
    payload: Mapping[str, Any]
    spec: Spec
    run_kwargs: Mapping[str, Any] = field(default_factory=dict)
    declarer: Declarer | None = None


class JobDefinition[S, P: Spec](ABC):
    """One kind of background job. Override what differs; the defaults are the common case."""

    #: Registry name, `<area>.<work>`.
    kind: ClassVar[str]
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: The Celery task this kind sends and recognises.
    task: ClassVar[str]
    queue: ClassVar[str | None] = None
    #: The model jobs are about; None: one job for the whole kind (maintenance) or run-only.
    subject_model: ClassVar[type | None] = None
    spec_model: ClassVar[type[Spec]] = EmptySpec
    #: Permission to apply or change a job of this kind (superuser in practice, #148's gate).
    permission: ClassVar[str] = "core.change_entry"
    #: False: runs are recorded, but there is no Job to declare (many runs per subject, e.g. e-mails).
    keyed: ClassVar[bool] = True
    #: Runs per generation before the job is failed.
    max_attempts: ClassVar[int] = 1
    #: The kind owns a deterministic task id (extraction): every run reuses one run row.
    stable_task_id: ClassVar[bool] = False
    #: A running run may be terminated when the job is cancelled (workers that write files: no).
    terminate_safe: ClassVar[bool] = False
    #: The worker reports its outcome (`POST /api/v1/jobs/runs/{run}/report`).
    reports: ClassVar[bool] = False
    #: Sweeper limit for a run without events; None: the catalog's or the service default.
    time_limit: ClassVar[timedelta | None] = None
    #: Domain models whose changes `observe()` reads (post_save → facade.observe).
    observes: ClassVar[tuple[type, ...]] = ()

    # -- subject --------------------------------------------------------------------------------------

    def scope(self, subject: S) -> Scope:
        return Scope()

    def subject_from_call(self, args: list, kwargs: Mapping[str, Any]) -> Any:
        """The subject's id read back from a task's arguments (runs sent outside the facade)."""
        return None

    def subject_of(self, instance: Any) -> S | None:
        """The subject an `observes` model instance belongs to."""
        return None

    # -- desire ---------------------------------------------------------------------------------------

    def check(self, subject: S, spec: P) -> Check:
        return Ready()

    def preview(self, subject: S, spec: P) -> Mapping[str, Any]:
        """What a dry run shows besides the check (e.g. the metadata a worker would receive)."""
        return {}

    def task_id_for(self, subject: S, spec: P) -> str | None:
        """The run id for `stable_task_id` kinds."""
        return None

    def args(self, dispatch: Dispatch[S, P]) -> list:
        return []

    def kwargs(self, dispatch: Dispatch[S, P]) -> dict:
        return {}

    def dispatch(self, dispatch: Dispatch[S, P]) -> None:
        """Send the run. Raise `Refused`-like `DispatchError` when nothing could be sent."""
        dispatch.send(self.task, args=self.args(dispatch), kwargs=self.kwargs(dispatch), queue=self.queue)

    # -- facts ----------------------------------------------------------------------------------------

    def observe(self, subject: S) -> Observation | None:
        """The job state the domain shows, for kinds whose truth lives in the domain (LCP)."""
        return None

    def discover(self) -> Iterable[S]:
        """Subjects that should have a job even if nobody applied one (they get one, without intent)."""
        return ()

    def on_report(self, report: Report[S]) -> Mapping[str, Any]:
        """Apply a worker's report; return what to store as the run's report."""
        raise DispatchError("This kind of job does not take reports.", status=400)

    # -- presentation ---------------------------------------------------------------------------------

    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "task": self.task,
            "subject_type": self.subject_model._meta.label_lower if self.subject_model else None,
            "keyed": self.keyed,
            "reports": self.reports,
            "terminate_safe": self.terminate_safe,
            "max_attempts": self.max_attempts,
            "spec_schema": self.spec_model.model_json_schema(),
        }


class DispatchError(Exception):
    """A refusal the API answers with `status` (409 conflict, 422 not eligible, 502 broker)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


__all__ = [
    "Declarer",
    "Dispatch",
    "DispatchError",
    "Dispatcher",
    "EmptySpec",
    "JobDefinition",
    "Report",
    "Spec",
]
