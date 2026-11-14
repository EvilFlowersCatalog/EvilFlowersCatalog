"""Ledger housekeeping (IP-031): the run sweeper and retention. Jobs are never pruned (one per subject)."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.tasks.application.registry import registry
from apps.tasks.domain.values import RunStatus
from apps.tasks.models import Job, JobEvent, JobRun

logger = logging.getLogger(__name__)

_SWEPT = (RunStatus.RECEIVED, RunStatus.STARTED, RunStatus.RETRYING)
_PRUNE_BATCH = 2000


def time_limit(run: JobRun) -> timedelta:
    """How long a started run may go without events before it is lost."""
    from evil_flowers_catalog.celery import app

    definition = registry.for_task(run.name)
    if definition is not None and definition.time_limit is not None:
        return definition.time_limit
    service = timedelta(hours=settings.EVILFLOWERS_JOBS_SERVICE_TIME_LIMIT_HOURS)
    if run.name in app.tasks:
        catalog = timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)
        # A retrying task waits for its countdown, which can exceed the run limit.
        return max(catalog, service) if run.status == RunStatus.RETRYING else catalog
    return service


def _lose(run: JobRun, now, reason: str) -> bool:
    updated = JobRun.objects.filter(pk=run.pk, status=run.status).update(
        status=RunStatus.LOST, finished_at=now, error=reason, last_event_at=now, updated_at=now
    )
    if updated:
        JobEvent.objects.create(run_id=run.pk, type="lost", timestamp=now, data={"previous": run.status})
        from apps.tasks.application.facade import facade

        run.refresh_from_db()
        transaction.on_commit(lambda: facade.run_changed(run))
    return bool(updated)


def sweep_lost_runs(now=None) -> int:
    now = now or timezone.now()
    grace = timedelta(minutes=settings.EVILFLOWERS_JOBS_LOST_GRACE_MINUTES)
    limits = [timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)] + [
        definition.time_limit for definition in registry.all() if definition.time_limit is not None
    ]
    candidates = (
        JobRun.objects.filter(status__in=_SWEPT)
        .annotate(last_seen=Coalesce("last_event_at", "started_at", "received_at", "created_at"))
        .filter(last_seen__lt=now - min(limits) - grace)
    )
    lost = 0
    for run in candidates.iterator():
        limit = time_limit(run)
        if run.last_seen < now - limit - grace:
            with transaction.atomic():
                lost += _lose(run, now, f"no event for {limit + grace} (worker lost or event missed)")

    # A queued run no worker ever announced: missed events (listener down, deploy) or lost in the broker.
    queued_limit = timedelta(hours=settings.EVILFLOWERS_JOBS_QUEUED_LOST_HOURS)
    for run in JobRun.objects.filter(status=RunStatus.QUEUED, created_at__lt=now - queued_limit).iterator():
        with transaction.atomic():
            lost += _lose(run, now, f"queued for more than {queued_limit} without any event")
    if lost:
        logger.warning("jobs.swept lost=%s", lost)
    return lost


def prune_runs(now=None) -> int:
    """Delete finished runs past retention; a job's current run always stays."""
    now = now or timezone.now()
    succeeded_cutoff = now - timedelta(days=settings.EVILFLOWERS_JOBS_RETENTION_DAYS)
    failed_cutoff = now - timedelta(days=settings.EVILFLOWERS_JOBS_RETENTION_FAILED_DAYS)
    expired = (
        JobRun.objects.filter(
            Q(status__in=(RunStatus.SUCCEEDED, RunStatus.REVOKED), created_at__lt=succeeded_cutoff)
            | Q(status__in=(RunStatus.FAILED, RunStatus.LOST, RunStatus.REJECTED), created_at__lt=failed_cutoff)
        )
        .exclude(id__in=Job.objects.filter(current_run__isnull=False).values("current_run_id"))
        .values_list("id", flat=True)
    )
    deleted = 0
    while batch := list(expired[:_PRUNE_BATCH]):
        with transaction.atomic():
            JobEvent.objects.filter(run_id__in=batch).delete()
            deleted += JobRun.objects.filter(id__in=batch).delete()[1].get("tasks.JobRun", 0)
    if deleted:
        logger.info("jobs.pruned deleted=%s", deleted)
    return deleted


__all__ = ["prune_runs", "sweep_lost_runs", "time_limit"]
