"""The background-jobs domain (IP-031 rev. 2): values, state machines, the provider contract.

Imports neither Django nor Celery (a test enforces it).
"""

from apps.tasks.domain.contract import (
    Declarer,
    Dispatch,
    DispatchError,
    Dispatcher,
    EmptySpec,
    JobDefinition,
    Report,
    Spec,
)
from apps.tasks.domain.values import (
    Blocked,
    Check,
    DesiredState,
    JobSnapshot,
    JobState,
    Observation,
    Ready,
    Refused,
    RunStatus,
    Scope,
)

__all__ = [
    "Blocked",
    "Check",
    "Declarer",
    "DesiredState",
    "Dispatch",
    "DispatchError",
    "Dispatcher",
    "EmptySpec",
    "JobDefinition",
    "JobSnapshot",
    "JobState",
    "Observation",
    "Ready",
    "Refused",
    "Report",
    "RunStatus",
    "Scope",
    "Spec",
]
