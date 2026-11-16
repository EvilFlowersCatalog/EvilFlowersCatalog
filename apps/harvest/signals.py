"""Outbound two-way sync (IP-034 Part B): entry changes enqueue `harvest.push` for every enabled `sync` source
whose target catalog holds the entry and whose connector can write.

Receivers only collect entry ids; one callback per transaction, after commit, declares the jobs, once per entry
and source while a push is pending (cache key, dropped when the push starts, like IP-033's OAI sync). Saves made
by the sync itself (`syncing()`) are skipped: that saves work, the merge base is the real loop guard.

These receivers run in every app's code paths and tests: they never raise.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterable

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.core.models import Entry, EntryAuthor

logger = logging.getLogger(__name__)

#: How long a pending push suppresses another one for the same entry and source.
DEDUPE_SECONDS = 60

#: Entry columns a connector can see (`apps.harvest.projection`). A save touching none of them (popularity,
#: `touched_at`, cover hashes, search columns) changes nothing remote.
SYNCED_COLUMNS = frozenset(
    {
        "title",
        "summary",
        "publisher",
        "published_at",
        "language",
        "language_id",
        "edition",
        "page_count",
        "identifiers",
        "catalog",
        "catalog_id",
    }
)

_state = threading.local()


@contextmanager
def syncing():
    """Writes inside come from the sync itself: no outbound push for them."""
    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1


def is_syncing() -> bool:
    return getattr(_state, "depth", 0) > 0


def dedupe_key(source_id, entry_id) -> str:
    return f"harvest:push:{source_id}:{entry_id}"


class _Batch:
    def __init__(self):
        self.ids: set[str] = set()
        self.flushed = False

    def flush(self):
        self.flushed = True
        enqueue(self.ids)


def schedule(entry_ids: Iterable) -> None:
    """Push these entries after the current transaction commits."""
    if is_syncing():
        return
    try:
        ids = {str(entry_id) for entry_id in entry_ids if entry_id}
        if not ids:
            return
        connection = transaction.get_connection()
        batch = getattr(connection, "_harvest_push_batch", None)
        if batch is None or batch.flushed or not any(item[1] == batch.flush for item in connection.run_on_commit):
            batch = _Batch()
            connection._harvest_push_batch = batch
            batch.ids.update(ids)
            transaction.on_commit(batch.flush, robust=True)
            return
        batch.ids.update(ids)
    except Exception:  # noqa: BLE001 — the source's scheduled run catches what is missed here
        logger.exception("harvest.schedule_push_failed")


def sync_sources_for(catalog_ids):
    """Enabled `sync` sources of these catalogs whose connector can write now."""
    from apps.harvest.adapters import registry
    from apps.harvest.models import HarvestSource

    sources = HarvestSource.objects.filter(
        mode=HarvestSource.Mode.SYNC, enabled=True, target_catalog_id__in=set(catalog_ids)
    )
    result = []
    for source in sources:
        connector = registry.get(source.adapter)
        if connector is not None and connector.can_write(source):
            result.append(source)
    return result


def enqueue(entry_ids: Iterable) -> int:
    """Declare `harvest.push` per entry and writable `sync` source of its catalog; the number declared."""
    declared = 0
    try:
        rows = list(Entry.objects.filter(pk__in=set(entry_ids)).values_list("pk", "catalog_id"))
        if not rows:
            return 0
        sources = sync_sources_for(catalog_id for _pk, catalog_id in rows)
        if not sources:
            return 0
        from apps.tasks.contract import DispatchError, declare

        for entry_id, catalog_id in rows:
            for source in sources:
                if source.target_catalog_id != catalog_id:
                    continue
                if not cache.add(dedupe_key(source.pk, entry_id), 1, DEDUPE_SECONDS):
                    continue
                try:
                    declare("harvest.push", entry_id, spec={"source": str(source.pk)})
                    declared += 1
                except DispatchError as error:
                    cache.delete(dedupe_key(source.pk, entry_id))
                    logger.warning("harvest.push_refused entry=%s source=%s: %s", entry_id, source.slug, error)
    except Exception:  # noqa: BLE001
        logger.exception("harvest.enqueue_push_failed")
    return declared


# -- receivers -----------------------------------------------------------------------------------------------


@receiver(post_save, sender=Entry, dispatch_uid="harvest.entry_saved")
def _entry_saved(sender, instance: Entry, created: bool, update_fields=None, raw=False, **kwargs):
    if raw:
        return
    if update_fields is not None and not (set(update_fields) & SYNCED_COLUMNS):
        return
    schedule([instance.pk])


@receiver(post_save, sender=EntryAuthor, dispatch_uid="harvest.entry_author_saved")
@receiver(post_delete, sender=EntryAuthor, dispatch_uid="harvest.entry_author_deleted")
def _entry_author_changed(sender, instance: EntryAuthor, raw=False, **kwargs):
    if not raw:
        schedule([instance.entry_id])


@receiver(m2m_changed, sender=Entry.categories.through, dispatch_uid="harvest.entry_categories_changed")
def _entry_categories_changed(sender, instance, action: str, reverse: bool, pk_set=None, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    schedule((pk_set or ()) if reverse else [instance.pk])


__all__ = ["DEDUPE_SECONDS", "SYNCED_COLUMNS", "dedupe_key", "enqueue", "is_syncing", "schedule", "syncing"]
