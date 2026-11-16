"""IP-033: signal receivers that keep `oai_records` in step.

A receiver only collects the affected entry ids. After the transaction commits, one callback per transaction
keeps the ids that can matter (entry in a harvestable catalog, or already a row) and enqueues
`apps.oai.tasks.sync_entries` for those with no sync pending (Q5: cache key `oai:sync:<id>`, 60 s; the task
drops the key when it starts, so an edit made while it runs gets its own sync). The task compares fingerprints,
so a reader opening a book (which saves the entry) costs a cache hit or one cheap task, never a new datestamp.

These receivers run in every app's code paths and tests: they never raise.
"""

import logging
from typing import Iterable

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete
from django.dispatch import receiver

from apps.core.models import Acquisition, Author, Catalog, Category, Entry, EntryAuthor

logger = logging.getLogger(__name__)

#: How long a pending sync suppresses another one for the same entry (Q5).
DEDUPE_SECONDS = 60

#: Entry ids per task message.
TASK_CHUNK = 500

#: Entry columns the projection or the exposure rule reads. A save whose `update_fields` touch none of them
#: (popularity, `touched_at`, cover hashes, search columns, ratings) cannot change the record.
PROJECTED_FIELDS = frozenset(
    {
        "title",
        "catalog",
        "catalog_id",
        "language",
        "language_id",
        "identifiers",
        "origin",
        "edition",
        "withdrawn_at",
        "published_at",
        "publisher",
        "summary",
        "config",
        "rights",
        "page_count",
    }
)


def dedupe_key(entry_id) -> str:
    return f"oai:sync:{entry_id}"


class _Batch:
    """The entry ids one transaction touched; flushed once, on commit."""

    def __init__(self):
        self.ids: set[str] = set()
        self.flushed = False

    def flush(self):
        self.flushed = True
        enqueue(self.ids)


def schedule(entry_ids: Iterable) -> None:
    """Sync these entries after the current transaction commits (at once outside a transaction)."""
    if not getattr(settings, "EVILFLOWERS_OAI_ENABLED", False):
        return
    try:
        ids = {str(entry_id) for entry_id in entry_ids if entry_id}
        if not ids:
            return
        connection = transaction.get_connection()
        batch = getattr(connection, "_oai_batch", None)
        # A batch already flushed, or whose callback was discarded (its savepoint or transaction rolled back),
        # is not reused.
        if batch is None or batch.flushed or not any(item[1] == batch.flush for item in connection.run_on_commit):
            batch = _Batch()
            connection._oai_batch = batch
            batch.ids.update(ids)
            transaction.on_commit(batch.flush, robust=True)
            return
        batch.ids.update(ids)
    except Exception:  # noqa: BLE001 — the nightly reconcile catches what is missed here
        logger.exception("oai.schedule_failed")


def _relevant(ids: set[str]) -> list[str]:
    """Ids that can have or need a record: in a harvestable catalog now, or with a row (live or tombstone)."""
    from apps.oai.models import OaiRecord

    relevant = {
        str(pk) for pk in Entry.objects.filter(pk__in=ids, catalog__harvestable=True).values_list("pk", flat=True)
    }
    relevant |= {str(pk) for pk in OaiRecord.objects.filter(entry_id__in=ids).values_list("entry_id", flat=True)}
    return sorted(relevant)


def enqueue(entry_ids: Iterable) -> None:
    fresh = []
    try:
        ids = _relevant({str(entry_id) for entry_id in entry_ids})
        fresh = [entry_id for entry_id in ids if cache.add(dedupe_key(entry_id), 1, DEDUPE_SECONDS)]
        if not fresh:
            return
        from apps.oai.tasks import sync_entries

        for start in range(0, len(fresh), TASK_CHUNK):
            sync_entries.delay(fresh[start : start + TASK_CHUNK])
    except Exception:  # noqa: BLE001
        logger.exception("oai.enqueue_failed entries=%s", len(fresh))
        try:
            cache.delete_many([dedupe_key(entry_id) for entry_id in fresh])
        except Exception:  # noqa: BLE001
            pass


# -- receivers -----------------------------------------------------------------------------------------------


@receiver(post_save, sender=Entry, dispatch_uid="oai.entry_saved")
def _entry_saved(sender, instance: Entry, created: bool, update_fields=None, raw=False, **kwargs):
    if raw:
        return
    if update_fields is not None and not (set(update_fields) & PROJECTED_FIELDS):
        return
    schedule([instance.pk])


@receiver(post_delete, sender=Entry, dispatch_uid="oai.entry_deleted")
def _entry_deleted(sender, instance: Entry, **kwargs):
    # The sync finds the entry gone and leaves a tombstone (also for each entry of a deleted catalog).
    schedule([instance.pk])


@receiver(post_save, sender=EntryAuthor, dispatch_uid="oai.entry_author_saved")
@receiver(post_delete, sender=EntryAuthor, dispatch_uid="oai.entry_author_deleted")
def _entry_author_changed(sender, instance: EntryAuthor, raw=False, **kwargs):
    if not raw:
        schedule([instance.entry_id])


@receiver(post_save, sender=Author, dispatch_uid="oai.author_saved")
def _author_saved(sender, instance: Author, created: bool, raw=False, **kwargs):
    if created or raw:
        return
    schedule(EntryAuthor.objects.filter(author_id=instance.pk).values_list("entry_id", flat=True))


@receiver(m2m_changed, sender=Entry.categories.through, dispatch_uid="oai.entry_categories_changed")
def _entry_categories_changed(sender, instance, action: str, reverse: bool, pk_set=None, **kwargs):
    if not reverse:
        if action in ("post_add", "post_remove", "post_clear"):
            schedule([instance.pk])
        return
    # From the category's side: `pk_set` holds entry ids; a clear names none, so read them before it.
    if action in ("post_add", "post_remove"):
        schedule(pk_set or ())
    elif action == "pre_clear":
        schedule(Entry.categories.through.objects.filter(category_id=instance.pk).values_list("entry_id", flat=True))


def _category_entries(category_id):
    return Entry.categories.through.objects.filter(category_id=category_id).values_list("entry_id", flat=True)


@receiver(post_save, sender=Category, dispatch_uid="oai.category_saved")
def _category_saved(sender, instance: Category, created: bool, raw=False, **kwargs):
    if created or raw:
        return
    schedule(_category_entries(instance.pk))


@receiver(pre_delete, sender=Category, dispatch_uid="oai.category_deleting")
def _category_deleting(sender, instance: Category, **kwargs):
    # `pre_delete`, not `post_delete`: the `entry_categories` rows are gone by then and no `m2m_changed` fires.
    schedule(_category_entries(instance.pk))


@receiver(post_save, sender=Catalog, dispatch_uid="oai.catalog_saved")
def _catalog_saved(sender, instance: Catalog, created: bool, raw=False, **kwargs):
    if created or raw:
        return
    entries = Entry.objects.filter(catalog_id=instance.pk).values_list("pk", flat=True)
    if not instance.harvestable:
        # Opted out (or never in): only entries that still have a live record need a tombstone.
        from apps.oai.models import OaiRecord

        entries = OaiRecord.objects.filter(entry_id__in=entries, deleted=False).values_list("entry_id", flat=True)
    schedule(entries)


@receiver(post_save, sender=Acquisition, dispatch_uid="oai.acquisition_saved")
@receiver(post_delete, sender=Acquisition, dispatch_uid="oai.acquisition_deleted")
def _acquisition_changed(sender, instance: Acquisition, raw=False, **kwargs):
    if not raw:
        schedule([instance.entry_id])


@receiver(post_save, sender="readium.EncryptedContent", dispatch_uid="oai.encrypted_content_saved")
def _encrypted_content_saved(sender, instance, raw=False, **kwargs):
    # An LCP title is exposed only once its package is registered (Q1).
    if not raw:
        schedule(Acquisition.objects.filter(pk=instance.acquisition_id).values_list("entry_id", flat=True))


__all__ = ["PROJECTED_FIELDS", "dedupe_key", "enqueue", "schedule"]
