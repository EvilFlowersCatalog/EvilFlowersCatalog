"""`/api/v1/jobs` (IP-031 rev. 2): jobs (declared and observed state), their runs, kinds, plans."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import AliasChoices, Field

from apps.api.serializers import Serializer

_RECENT_RUNS = 20
_RECENT_EVENTS = 100


class RunSerializer:
    class Base(Serializer):
        id: UUID
        job_id: Optional[UUID]
        name: str = Field(description="Celery task name.")
        kind: Optional[str]
        status: str = Field(
            description="queued, received, started, retrying, succeeded, failed, revoked, rejected, lost."
        )
        parent_id: Optional[UUID]
        queue: Optional[str]
        worker: Optional[str]
        attempts: int
        error: Optional[str]
        runtime: Optional[float]
        trace_id: Optional[str]
        created_at: datetime
        sent_at: Optional[datetime]
        received_at: Optional[datetime]
        started_at: Optional[datetime]
        finished_at: Optional[datetime]
        reported_at: Optional[datetime]

    class Detail(Base):
        job_args: Any
        job_kwargs: Any
        args_exact: bool
        result: Any
        traceback: Optional[str]
        report: Any

    class Event(Serializer):
        type: str
        timestamp: datetime
        hostname: Optional[str]
        data: dict


class JobSerializer:
    class Base(Serializer):
        """A job: what was declared (spec, desired_state, generation) and what is observed (state)."""

        id: UUID
        kind: str
        subject_type: Optional[str] = Field(
            validation_alias=AliasChoices("subject_label", "subject_type"), description="`app_label.model`."
        )
        subject_id: Optional[UUID]
        catalog_id: Optional[UUID]
        entry_id: Optional[UUID]
        spec: dict
        desired_state: str = Field(description="completed or cancelled: what a client declared.")
        state: str = Field(
            description="pending, blocked, queued, running, cancelling, succeeded, failed, cancelled: what is observed."
        )
        stale: bool = Field(description="Queued for longer than EVILFLOWERS_JOBS_STALE_MINUTES.")
        reason: Optional[str] = Field(description="Why the job is blocked or failed.")
        generation: int = Field(description="Bumped by every apply that asks for a new run.")
        observed_generation: int
        attempts: int = Field(description="Runs of the current generation.")
        detail: dict = Field(description="What the provider observed in the domain (e.g. `encryption`).")
        current_run: Optional[RunSerializer.Base]
        cause_id: Optional[UUID] = Field(description="The job whose outcome declared this one.")
        requested_by_id: Optional[UUID]
        created_at: datetime
        updated_at: datetime
        last_transition_at: Optional[datetime]

    class RunCount(Serializer):
        name: str
        status: str
        count: int

    class Detail(Base):
        runs: list[RunSerializer.Base] = Field(
            description=f"The job's own runs, newest first (at most {_RECENT_RUNS})."
        )
        run_counts: list["JobSerializer.RunCount"] = Field(
            description="Every run of the job (its follow-up tasks too) counted per task name and status (#150)."
        )
        events: list[RunSerializer.Event] = Field(description=f"Transitions, newest last (at most {_RECENT_EVENTS}).")
        consequences: list["JobSerializer.Base"] = Field(description="Jobs this job's outcome declared.")

        @classmethod
        def from_job(cls, job) -> "JobSerializer.Detail":
            # Views call this by name so `apps.openapi` types the response.
            from django.db.models import Count

            base = JobSerializer.Base.model_validate(job).model_dump()
            own_runs = job.runs.filter(kind=job.kind).order_by("-created_at")
            counts = job.runs.values("name", "status").annotate(count=Count("id")).order_by("name", "status")
            events = list(job.events.order_by("-timestamp")[:_RECENT_EVENTS])[::-1]
            return cls.model_validate(
                {
                    **base,
                    "runs": [RunSerializer.Base.model_validate(run) for run in own_runs[:_RECENT_RUNS]],
                    "run_counts": list(counts),
                    "events": [RunSerializer.Event.model_validate(event) for event in events],
                    "consequences": [JobSerializer.Base.model_validate(child) for child in job.consequences.all()],
                }
            )

    class Plan(Serializer):
        """`POST /api/v1/jobs?dry_run=true`: what the apply would do; nothing stored or sent."""

        kind: str
        subject_id: Optional[UUID]
        spec: dict
        outcome: str = Field(description="would-run, would-block or refused.")
        reason: Optional[str]
        preview: dict
        job: Optional["JobSerializer.Base"] = Field(description="The job as it is now, if there is one.")

        @classmethod
        def from_plan(cls, plan) -> "JobSerializer.Plan":
            return cls.model_validate(
                {
                    "kind": plan.kind,
                    "subject_id": plan.subject_id,
                    "spec": dict(plan.spec),
                    "outcome": plan.outcome,
                    "reason": plan.reason,
                    "preview": dict(plan.preview),
                    "job": JobSerializer.Base.model_validate(plan.job) if plan.job else None,
                }
            )

    class Kind(Serializer):
        kind: str
        title: str
        description: str
        task: str
        subject_type: Optional[str]
        keyed: bool = Field(description="False: run-only kind (recorded, never declared).")
        outcome_file: bool = Field(description="The worker writes its outcome to shared storage (#169).")
        terminate_safe: bool
        max_attempts: int
        spec_schema: dict

    class Summary(Serializer):
        kind: str
        states: dict
        stale: int


JobSerializer.Detail.model_rebuild()
JobSerializer.Plan.model_rebuild()
