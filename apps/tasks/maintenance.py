"""Ledger housekeeping (IP-031): the sweeper and retention."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.tasks import ledger
from apps.tasks.kinds import registry
from apps.tasks.models import JobEvent, JobProtocol

logger = logging.getLogger(__name__)

Status = JobProtocol.JobStatus

_SWEPT = (Status.RECEIVED, Status.STARTED, Status.RETRYING)
_PRUNE_BATCH = 2000


def time_limit(job: JobProtocol) -> timedelta:
    """How long a started job may go without events before it is lost."""
    from evil_flowers_catalog.celery import app

    kind = registry.get(job.kind)
    if kind is not None and kind.time_limit is not None:
        return kind.time_limit
    service = timedelta(hours=settings.EVILFLOWERS_JOBS_SERVICE_TIME_LIMIT_HOURS)
    if job.name in app.tasks:
        catalog = timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)
        # A retrying task waits for its countdown, which can exceed the run limit.
        return max(catalog, service) if job.status == Status.RETRYING else catalog
    return service


def sweep_lost_jobs(now=None) -> int:
    now = now or timezone.now()
    grace = timedelta(minutes=settings.EVILFLOWERS_JOBS_LOST_GRACE_MINUTES)
    shortest = min(
        [timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)]
        + [kind.time_limit for kind in registry.all() if kind.time_limit is not None]
    )
    candidates = (
        JobProtocol.objects.filter(status__in=_SWEPT)
        .annotate(last_seen=Coalesce("last_event_at", "started_at", "received_at", "created_at"))
        .filter(last_seen__lt=now - shortest - grace)
    )
    lost = 0
    for job in candidates.iterator():
        limit = time_limit(job)
        if job.last_seen >= now - limit - grace:
            continue
        with transaction.atomic():
            updated = JobProtocol.objects.filter(pk=job.pk, status=job.status, last_event_at=job.last_event_at).update(
                status=Status.LOST,
                finished_at=now,
                error=f"no event for {limit + grace} (worker lost or event missed)",
                updated_at=now,
            )
            if updated:
                ledger.log(job, "lost", limit_seconds=int(limit.total_seconds()), previous=job.status)
                lost += 1
    # A queued job no worker ever announced: its events were missed (listener down, deploy) or the task
    # was lost in the broker. Waited long enough for any backlog to drain, it stops counting as active.
    queued_limit = timedelta(hours=settings.EVILFLOWERS_JOBS_QUEUED_LOST_HOURS)
    for job in JobProtocol.objects.filter(
        status=Status.QUEUED, last_event_at__isnull=True, created_at__lt=now - queued_limit
    ).iterator():
        updated = JobProtocol.objects.filter(pk=job.pk, status=Status.QUEUED).update(
            status=Status.LOST,
            finished_at=now,
            error=f"queued for more than {queued_limit} without any event",
            updated_at=now,
        )
        if updated:
            ledger.log(job, "lost", previous=Status.QUEUED)
            lost += 1
    if lost:
        logger.warning("jobs.swept lost=%s", lost)
    return lost


def prune_jobs(now=None) -> int:
    from apps.tasks.services import latest_ids

    now = now or timezone.now()
    succeeded_cutoff = now - timedelta(days=settings.EVILFLOWERS_JOBS_RETENTION_DAYS)
    failed_cutoff = now - timedelta(days=settings.EVILFLOWERS_JOBS_RETENTION_FAILED_DAYS)
    expired = (
        JobProtocol.objects.filter(
            Q(status__in=(Status.SUCCEEDED, Status.REVOKED), created_at__lt=succeeded_cutoff)
            | Q(status__in=(Status.FAILED, Status.LOST, Status.REJECTED), created_at__lt=failed_cutoff)
        )
        .exclude(id__in=latest_ids())
        .values_list("id", flat=True)
    )
    deleted = 0
    while True:
        batch = list(expired[:_PRUNE_BATCH])
        if not batch:
            break
        with transaction.atomic():
            JobEvent.objects.filter(job_id__in=batch).delete()
            deleted += JobProtocol.objects.filter(id__in=batch).delete()[1].get("tasks.JobProtocol", 0)
    if deleted:
        logger.info("jobs.pruned deleted=%s", deleted)
    return deleted


__all__ = ["prune_jobs", "sweep_lost_jobs", "time_limit"]
