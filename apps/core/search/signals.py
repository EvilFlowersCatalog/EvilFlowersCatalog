"""
Keep the search columns current: every write that changes indexed text refreshes the affected entries'
lexical index at once and queues their vectors (IP-021) after commit.
"""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.core.models import Author, Category, Entry, EntryAuthor
from apps.core.search import index, vectors


def refresh_search_vectors(entry_ids) -> None:
    entry_ids = list(entry_ids)
    if index.refresh_search_vectors(entry_ids):
        vectors.schedule(entry_ids)


@receiver(post_save, sender=Entry, dispatch_uid="search_entry_saved")
def entry_saved(sender, instance: Entry, raw=False, **kwargs):
    if not raw:
        refresh_search_vectors([instance.pk])


@receiver(post_save, sender=EntryAuthor, dispatch_uid="search_entry_author_saved")
@receiver(post_delete, sender=EntryAuthor, dispatch_uid="search_entry_author_deleted")
def entry_author_changed(sender, instance: EntryAuthor, raw=False, **kwargs):
    if not raw:
        refresh_search_vectors([instance.entry_id])


@receiver(post_save, sender=Author, dispatch_uid="search_author_saved")
def author_saved(sender, instance: Author, raw=False, created=False, **kwargs):
    if not raw and not created:
        refresh_search_vectors(EntryAuthor.objects.filter(author=instance).values_list("entry_id", flat=True))


@receiver(post_save, sender=Category, dispatch_uid="search_category_saved")
def category_saved(sender, instance: Category, raw=False, created=False, **kwargs):
    if not raw and not created:
        refresh_search_vectors(instance.entries.values_list("pk", flat=True))


def _relation_changed(instance, action, reverse, pk_set, **kwargs):
    """`m2m_changed` for Entry.categories / Entry.authors, from either side of the relation."""
    if action == "pre_clear" and reverse:
        # The reverse side (`category.entries.clear()`) does not report which entries it removes.
        instance._search_cleared_entries = list(instance.entries.values_list("pk", flat=True))
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    if not reverse:
        refresh_search_vectors([instance.pk])
    elif action == "post_clear":
        refresh_search_vectors(getattr(instance, "_search_cleared_entries", []))
    else:
        refresh_search_vectors(pk_set or [])


@receiver(m2m_changed, sender=Entry.categories.through, dispatch_uid="search_entry_categories_changed")
def entry_categories_changed(sender, **kwargs):
    _relation_changed(**kwargs)


@receiver(m2m_changed, sender=Entry.authors.through, dispatch_uid="search_entry_authors_changed")
def entry_authors_changed(sender, **kwargs):
    _relation_changed(**kwargs)
