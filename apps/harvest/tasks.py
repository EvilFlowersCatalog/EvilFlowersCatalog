"""Celery tasks of the harvesting client (IP-034): a `harvest.run` job's run, and the beat tick."""

import logging

from celery import shared_task

from apps.harvest.jobs import TIME_LIMIT

logger = logging.getLogger(__name__)


@shared_task(time_limit=int(TIME_LIMIT.total_seconds()), soft_time_limit=int(TIME_LIMIT.total_seconds()) - 120)
def run(source_id: str, dry_run: bool = False, full: bool = False) -> dict:
    """One run of a `harvest.run` job. The returned counters are the run's result (`JobRun.result`), and the
    source keeps them as `last_report` for catalog managers."""
    from apps.harvest.models import HarvestSource
    from apps.harvest.runner import run_source

    source = HarvestSource.objects.select_related("target_catalog", "creator").get(pk=source_id)
    return run_source(source, dry_run=dry_run, full=full)


@shared_task(time_limit=600, soft_time_limit=540)
def push(entry_id: str, source_id: str) -> dict:
    """A `harvest.push` run: merge the entry with its remote record and write it there (or create it)."""
    from django.core.cache import cache

    from apps.core.models import Entry
    from apps.harvest.models import HarvestSource
    from apps.harvest.signals import dedupe_key
    from apps.harvest.sync import push as push_entry

    # Dropped first: an edit made while this runs gets its own push.
    cache.delete(dedupe_key(source_id, entry_id))
    entry = Entry.objects.select_related("language", "catalog").get(pk=entry_id)
    source = HarvestSource.objects.select_related("target_catalog", "creator").get(pk=source_id)
    return push_entry(entry, source).as_dict()


@shared_task(time_limit=600, soft_time_limit=540)
def pull(entry_id: str, source_id: str) -> dict:
    """A `harvest.pull` run: fetch the entry's remote record and merge it in."""
    from apps.core.models import Entry
    from apps.harvest.models import HarvestSource
    from apps.harvest.sync import pull as pull_entry

    entry = Entry.objects.get(pk=entry_id)
    source = HarvestSource.objects.select_related("target_catalog", "creator").get(pk=source_id)
    outcome = pull_entry(entry, source)
    return {"status": outcome.status, "message": outcome.message, "changed": outcome.changed}


@shared_task
def tick() -> dict:
    """Beat (EVILFLOWERS_HARVEST_TICK_SCHEDULE): declare `harvest.run` for every enabled source whose own schedule
    fired since its last run. Declaring the same spec on a job still running changes nothing (IP-031 Q12)."""
    from django.utils import timezone

    from apps.harvest.models import HarvestSource
    from apps.harvest.schedule import is_due
    from apps.tasks.contract import DispatchError, declare

    now = timezone.now()
    declared, refused = [], []
    for source in HarvestSource.objects.filter(enabled=True).exclude(schedule=""):
        if not is_due(source, now=now):
            continue
        try:
            declare("harvest.run", source.pk, spec={"dry_run": False, "full": False})
        except DispatchError as error:
            logger.warning("harvest.tick source=%s refused: %s", source.slug, error.message)
            refused.append(source.slug)
            continue
        declared.append(source.slug)
    return {"declared": declared, "refused": refused}


__all__ = ["pull", "push", "run", "tick"]
