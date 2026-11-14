from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import AliasChoices, Field

from apps.api.serializers import Serializer


class JobSerializer:
    class Brief(Serializer):
        id: UUID
        kind: Optional[str]
        name: str
        status: str
        created_at: datetime
        finished_at: Optional[datetime]

    class Base(Serializer):
        """A job in a list: what it is, where it is, whether it needs attention."""

        id: UUID
        kind: Optional[str] = Field(description="Job kind (`GET /api/v1/jobs/kinds`); null for tasks without one.")
        name: str = Field(description="Celery task name.")
        status: str = Field(
            description="queued, received, started, retrying, succeeded, failed, revoked, rejected or lost."
        )
        stale: bool = Field(description="Queued for longer than EVILFLOWERS_JOBS_STALE_MINUTES.")
        queue: Optional[str]
        worker: Optional[str]
        subject_type: Optional[str] = Field(
            validation_alias=AliasChoices("subject_label", "subject_type"), description="`app_label.model`."
        )
        subject_id: Optional[UUID]
        catalog_id: Optional[UUID]
        entry_id: Optional[UUID]
        parent_id: Optional[UUID]
        retry_of_id: Optional[UUID]
        requested_by_id: Optional[UUID]
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
        revoke_requested_at: Optional[datetime]

    class Event(Serializer):
        type: str
        timestamp: datetime
        hostname: Optional[str]
        data: dict

    class Detail(Base):
        options: dict
        job_args: Any
        job_kwargs: Any
        args_exact: bool
        result: Any
        traceback: Optional[str]
        report: Any
        events: List["JobSerializer.Event"] = []
        children: List["JobSerializer.Brief"] = []
        retries: List["JobSerializer.Brief"] = []
        detail: dict = Field(default_factory=dict, description="Kind-specific fields, e.g. `encryption` (#148).")

    class Kind(Serializer):
        name: str
        title: str
        description: str
        task: str
        subject_type: Optional[str]
        startable: bool
        exclusive: bool
        reports: bool
        terminate_safe: bool
        options_schema: dict
        actions: List[dict]

    class Summary(Serializer):
        kind: Optional[str]
        statuses: dict
        stale: int


JobSerializer.Detail.model_rebuild()


def job_detail(job) -> "JobSerializer.Detail":
    """The full job: events, children, retries and the kind's own fields."""
    from apps.tasks.kinds import registry

    base = JobSerializer.Base.model_validate(job).model_dump()
    kind = registry.get(job.kind)
    detail = {}
    if kind is not None:
        try:
            detail = kind.detail(job, kind.get_subject(job.subject_id)) or {}
        except Exception:  # noqa: BLE001 — a kind's extra fields must not hide the job itself
            detail = {"error": "detail unavailable"}
    return JobSerializer.Detail.model_validate(
        {
            **base,
            "options": job.options or {},
            "job_args": job.job_args,
            "job_kwargs": job.job_kwargs,
            "args_exact": job.args_exact,
            "result": job.result,
            "traceback": job.traceback,
            "report": job.report,
            "events": [JobSerializer.Event.model_validate(event) for event in job.events.order_by("timestamp")[:500]],
            "children": [JobSerializer.Brief.model_validate(child) for child in job.children.order_by("created_at")],
            "retries": [JobSerializer.Brief.model_validate(retry) for retry in job.retries.order_by("created_at")],
            "detail": detail,
        }
    )
