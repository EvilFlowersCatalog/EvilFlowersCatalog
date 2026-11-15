"""Capability-token scopes for background jobs (#169).

A worker that writes its outcome to shared storage (`JobDefinition.outcome_file`) copies the token its
task carried into the file. The catalog applies the file only when the token is valid under
`JOB_RUN_OUTCOME` and names the same run. The token binds the file to the one run the catalog sent,
for a limited time and once. It does not authenticate a caller: it sits in the broker next to the
task message.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings

from apps.core.services.capability_tokens import CapabilityTokenService

# Single use, consumed when the outcome is applied (never when the run starts: a redelivered message
# carries the same token).
JOB_RUN_OUTCOME = "job_run_outcome"

#: `sub` of a token for a job nobody requested (declared by the system, e.g. an upload).
SYSTEM_SUBJECT = "system"


def outcome_token_ttl(time_limit: timedelta | None) -> int:
    """Seconds an outcome token lives: queue wait plus the run plus the sweeper's grace.

    Past that the sweeper calls the run lost anyway. `EVILFLOWERS_JOBS_OUTCOME_TOKEN_TTL_SECONDS` overrides it.
    """
    if settings.EVILFLOWERS_JOBS_OUTCOME_TOKEN_TTL_SECONDS:
        return int(settings.EVILFLOWERS_JOBS_OUTCOME_TOKEN_TTL_SECONDS)
    run = time_limit or timedelta(hours=settings.EVILFLOWERS_JOBS_SERVICE_TIME_LIMIT_HOURS)
    total = (
        timedelta(hours=settings.EVILFLOWERS_JOBS_QUEUED_LOST_HOURS)
        + run
        + timedelta(minutes=settings.EVILFLOWERS_JOBS_LOST_GRACE_MINUTES)
    )
    return int(total.total_seconds())


def mint_outcome_token(*, job_id, run_id, kind: str, time_limit: timedelta | None) -> str:
    from apps.tasks.models import Job

    requested_by = Job.objects.filter(pk=job_id).values_list("requested_by_id", flat=True).first()
    return CapabilityTokenService.mint(
        scope=JOB_RUN_OUTCOME,
        subject={
            "sub": str(requested_by) if requested_by else SYSTEM_SUBJECT,
            "resource_id": str(run_id),
            "kind": kind,
        },
        ttl=outcome_token_ttl(time_limit),
        single_use=True,
    )


__all__ = ["JOB_RUN_OUTCOME", "SYSTEM_SUBJECT", "mint_outcome_token", "outcome_token_ttl"]
