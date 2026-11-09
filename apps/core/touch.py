"""
Bump `touched_at` on changes that do not save the entry itself (#72).

OPDS feed ETags (`apps/opds/services/conditional.py`) cover every entry on a page
as `(pk, touched_at)`. Saving an entry or an acquisition bumps it, but a new
author or category, a renamed author or category, or a deleted acquisition
changes what the feed renders without saving the entry, so a client kept
getting `304` for a stale page.

The receivers use `.update()`: no `post_save` cascade, and a no-op for an entry
that is itself being deleted.
"""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.core.models import Acquisition, Author, Catalog, Category, Entry, EntryAuthor, Feed


def touch_entries(entry_ids) -> None:
    """Mark these entries, their catalogs and the feeds listing them as changed now."""
    entry_ids = list(entry_ids)
    if not entry_ids:
        return
    now = timezone.now()
    Entry.objects.filter(pk__in=entry_ids).update(touched_at=now)
    Catalog.objects.filter(entries__in=entry_ids).update(touched_at=now)
    Feed.objects.filter(entries__in=entry_ids).update(touched_at=now)


@receiver(post_delete, sender=Acquisition, dispatch_uid="touch_acquisition_deleted")
def acquisition_deleted(sender, instance: Acquisition, **kwargs):
    touch_entries([instance.entry_id])


@receiver(post_save, sender=EntryAuthor, dispatch_uid="touch_entry_author_saved")
@receiver(post_delete, sender=EntryAuthor, dispatch_uid="touch_entry_author_deleted")
def entry_author_changed(sender, instance: EntryAuthor, raw=False, **kwargs):
    if not raw:
        touch_entries([instance.entry_id])


@receiver(post_save, sender=Author, dispatch_uid="touch_author_saved")
def author_saved(sender, instance: Author, raw=False, created=False, **kwargs):
    if not raw and not created:
        touch_entries(EntryAuthor.objects.filter(author=instance).values_list("entry_id", flat=True))


@receiver(post_save, sender=Category, dispatch_uid="touch_category_saved")
def category_saved(sender, instance: Category, raw=False, created=False, **kwargs):
    if not raw and not created:
        touch_entries(instance.entries.values_list("pk", flat=True))


def _relation_changed(instance, action, reverse, pk_set, **kwargs):
    """`m2m_changed` for Entry.categories / Entry.authors, from either side of the relation."""
    if action == "pre_clear" and reverse:
        # The reverse side (`category.entries.clear()`) does not report which entries it removes.
        instance._touch_cleared_entries = list(instance.entries.values_list("pk", flat=True))
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    if not reverse:
        touch_entries([instance.pk])
    elif action == "post_clear":
        touch_entries(getattr(instance, "_touch_cleared_entries", []))
    else:
        touch_entries(pk_set or [])


@receiver(m2m_changed, sender=Entry.categories.through, dispatch_uid="touch_entry_categories_changed")
@receiver(m2m_changed, sender=Entry.authors.through, dispatch_uid="touch_entry_authors_changed")
def entry_relation_changed(sender, **kwargs):
    _relation_changed(**kwargs)
