"""Values of the background-jobs domain (IP-031 rev. 2). No Django, no Celery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class JobState(StrEnum):
    """Observed state of a job (diagram `state-job`)."""

    PENDING = "pending"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def waiting(self) -> bool:
        return self in (JobState.PENDING, JobState.BLOCKED)

    @property
    def active(self) -> bool:
        return self in (JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING)

    @property
    def settled(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class DesiredState(StrEnum):
    """What a client wants: the job done (for the current generation), or nothing running."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """Status of one Celery task execution (diagram `state-job-run`)."""

    QUEUED = "queued"
    RECEIVED = "received"
    STARTED = "started"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVOKED = "revoked"
    REJECTED = "rejected"
    LOST = "lost"

    @property
    def active(self) -> bool:
        return self in (RunStatus.QUEUED, RunStatus.RECEIVED, RunStatus.STARTED, RunStatus.RETRYING)

    @property
    def final(self) -> bool:
        return not self.active


# -- definition answers ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ready:
    """The job can run now."""


@dataclass(frozen=True, slots=True)
class Blocked:
    """It cannot run now but may later (LCP off, active loans): the job waits in `blocked`."""

    reason: str


@dataclass(frozen=True, slots=True)
class Refused:
    """It can never run on this subject (wrong file type, no file): no job, 422."""

    reason: str


type Check = Ready | Blocked | Refused


@dataclass(frozen=True, slots=True)
class Observation:
    """A job state read from the domain (e.g. `EncryptedContent.status`), and details worth listing."""

    state: JobState
    reason: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    #: When the domain entered this state (e.g. the package's last dispatch); `stale` counts from it.
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class Scope:
    """Where a subject lives, for filtering and access."""

    catalog_id: Any = None
    entry_id: Any = None


# -- snapshots the state machine decides on -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    state: JobState
    desired: DesiredState
    generation: int
    observed_generation: int
    attempts: int
    max_attempts: int
    run_status: RunStatus | None = None
    last_transition_at: datetime | None = None

    @property
    def behind(self) -> bool:
        """A generation the reconciler has not acted on yet."""
        return self.generation > self.observed_generation

    @property
    def retry_due(self) -> bool:
        return self.state is JobState.PENDING and not self.behind and self.attempts < self.max_attempts


__all__ = [
    "Blocked",
    "Check",
    "DesiredState",
    "JobSnapshot",
    "JobState",
    "Observation",
    "Ready",
    "Refused",
    "RunStatus",
    "Scope",
]
