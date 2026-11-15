"""The two state machines of IP-031 rev. 2, as pure functions (diagrams `state-job`, `state-job-run`).

Nothing here touches a database, a broker or a clock: the application layer loads snapshots, asks
these functions what to do, and carries it out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from apps.tasks.domain.values import (
    Blocked,
    Check,
    DesiredState,
    JobSnapshot,
    JobState,
    Ready,
    Refused,
    RunStatus,
)

# -- job: reconcile ----------------------------------------------------------------------------------


class Action(StrEnum):
    """What the reconciler must do for one job."""

    NOTHING = "nothing"
    CHECK = "check"  # ask the definition; Ready → dispatch a run, Blocked → blocked, Refused → failed
    REVOKE = "revoke"  # the run never starts: revoke it, the job is cancelled
    STOP = "stop"  # the run is running: cancelling (terminated when the definition allows it)
    CANCEL = "cancel"  # nothing runs: the job is cancelled


def reconcile(job: JobSnapshot) -> Action:
    if job.desired is DesiredState.CANCELLED:
        match job.state:
            case JobState.PENDING | JobState.BLOCKED:
                return Action.CANCEL
            case JobState.QUEUED:
                return Action.REVOKE
            case JobState.RUNNING:
                return Action.STOP
            case JobState.CANCELLING if job.run_status is None or job.run_status.final:
                return Action.CANCEL
            case _:
                return Action.NOTHING

    if job.state.active:
        return Action.NOTHING
    if job.state is JobState.BLOCKED:
        return Action.CHECK
    if job.state is JobState.PENDING and (job.behind or job.retry_due):
        return Action.CHECK
    if job.state.settled and job.behind:
        return Action.CHECK
    return Action.NOTHING


def after_check(check: Check) -> JobState:
    """The state a CHECK leads to; QUEUED means: dispatch a run now."""
    match check:
        case Ready():
            return JobState.QUEUED
        case Blocked():
            return JobState.BLOCKED
        case Refused():
            return JobState.FAILED


# -- job: apply ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    new_generation: bool
    state: JobState


def apply(job: JobSnapshot | None, *, desired: DesiredState, spec_changed: bool, stale: bool) -> ApplyOutcome:
    """A client's apply (or patch). Idempotent: the same spec on an active, non-stale job changes nothing (Q12)."""
    if job is None:
        if desired is DesiredState.CANCELLED:
            return ApplyOutcome(False, JobState.CANCELLED)
        return ApplyOutcome(True, JobState.PENDING)

    if desired is DesiredState.CANCELLED:
        # The reconciler takes it from here (cancel, revoke or stop); no new generation.
        return ApplyOutcome(False, job.state)

    resumed = job.desired is DesiredState.CANCELLED
    if job.state.settled or spec_changed or stale or resumed:
        # An active job keeps running; its run's end finds the job behind and starts the new generation.
        # A stale one does not wait for a run that almost certainly never reached a worker.
        keep = job.state.active and not stale
        return ApplyOutcome(True, job.state if keep else JobState.PENDING)
    return ApplyOutcome(False, job.state)


# -- job: a run moved ---------------------------------------------------------------------------------


def on_run(job: JobSnapshot, run: RunStatus) -> JobState:
    """The job state a run status leads to."""
    if job.desired is DesiredState.CANCELLED:
        return JobState.CANCELLING if run.active else JobState.CANCELLED

    if run.final and job.behind:
        # A new generation was applied while this run was going: it is next.
        return JobState.PENDING

    match run:
        case RunStatus.QUEUED | RunStatus.RECEIVED:
            return JobState.QUEUED
        case RunStatus.STARTED | RunStatus.RETRYING:
            return JobState.RUNNING
        case RunStatus.SUCCEEDED:
            return JobState.SUCCEEDED
        case RunStatus.REVOKED:
            return JobState.CANCELLED
        case RunStatus.FAILED | RunStatus.REJECTED | RunStatus.LOST:
            return JobState.PENDING if job.attempts < job.max_attempts else JobState.FAILED


def settle_with_observation(run_state: JobState, observed: JobState | None) -> JobState:
    """The domain may hold back a run's success, never mask its failure.

    A succeeded lcpencrypt run whose package is still pending is not a succeeded job (the job follows the
    domain); a failed run stays a failed job even if an older package is registered (the declared work
    did not happen). A later domain change can still settle the job through `observe()`.
    """
    if observed is None or run_state is not JobState.SUCCEEDED:
        return run_state
    return observed


OUTCOME_SUCCEEDED = ("done", "skipped")


def run_after_outcome(current: RunStatus, outcome: str | None) -> RunStatus | None:
    """The run status an outcome file implies (#169); None: keep the run as it is.

    The worker writes the file last, so it is the truth about how the run ended: a `lost` run whose file
    says `done` succeeded, and a succeeded run whose file was rejected (`outcome=None`) failed. A revoked
    run stays revoked; its outcome is discarded, never applied.
    """
    if current is RunStatus.REVOKED or not current.final:
        return None
    target = RunStatus.SUCCEEDED if outcome in OUTCOME_SUCCEEDED else RunStatus.FAILED
    return None if target is current else target


# -- run: facts ------------------------------------------------------------------------------------


def run_accepts(current: RunStatus | None, new: RunStatus, *, newer: bool) -> bool:
    """One precedence rule for run facts.

    An event older than the run's last event is history only; `revoked` is final; any other final
    status reopens only on a newer received/started (a redelivery, a Celery retry, a re-queue under a
    stable task id); `queued` only from nothing.
    """
    if current is None:
        return True
    if not newer:
        return False
    if current is RunStatus.REVOKED:
        return new is RunStatus.REVOKED
    if new is RunStatus.QUEUED:
        return current is RunStatus.QUEUED
    if current.final:
        return new in (RunStatus.RECEIVED, RunStatus.STARTED) or new is current
    if new is RunStatus.RETRYING:
        return current in (RunStatus.RECEIVED, RunStatus.STARTED, RunStatus.RETRYING)
    return True


__all__ = [
    "Action",
    "ApplyOutcome",
    "after_check",
    "apply",
    "on_run",
    "reconcile",
    "run_accepts",
    "run_after_outcome",
    "settle_with_observation",
]
