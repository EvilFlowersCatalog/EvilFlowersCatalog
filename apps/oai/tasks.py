"""IP-033 background work: `sync_entries` after edits (signals), `reconcile` nightly (beat, maintenance queue)."""

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Seconds the nightly reconcile may run; ~1000 entries take well under a minute.
RECONCILE_TIME_LIMIT = 30 * 60


@shared_task(ignore_result=True)
def sync_entries(entry_ids) -> dict:
    """Re-project these entries; write only changed records, tombstone the gone or hidden ones."""
    from apps.oai.signals import dedupe_key
    from apps.oai.sync import OaiRecordSync

    ids = [str(entry_id) for entry_id in entry_ids or ()]
    # Before reading the entries: a change committed from now on must enqueue a sync of its own.
    cache.delete_many([dedupe_key(entry_id) for entry_id in ids])
    result = OaiRecordSync().sync(ids)
    logger.info("oai.sync entries=%s %s", len(ids), result.as_dict())
    return result.as_dict()


@shared_task(ignore_result=True, time_limit=RECONCILE_TIME_LIMIT, soft_time_limit=RECONCILE_TIME_LIMIT - 60)
def reconcile() -> dict:
    """Re-project every exposed entry and tombstone every live row whose entry is gone or hidden."""
    from apps.oai.sync import OaiRecordSync

    if not settings.EVILFLOWERS_OAI_ENABLED:
        return {"skipped": "EVILFLOWERS_OAI_ENABLED is off"}
    return OaiRecordSync().reconcile().as_dict()
