"""
Remove stored files once the rows that own them are gone.

Django deletes rows, not files. Before these receivers existed, deleting an entry,
an acquisition or a whole catalog (REST, MCP, admin, a cascade) left every book,
cover, thumbnail, extraction crop and LCP package behind in storage — the
OpenAPI description of `DELETE .../entries/{id}` promised otherwise.

Three rules keep this safe:

- **Receivers, not `delete()` overrides.** `QuerySet.delete()` and FK cascades
  (`Catalog` → `Entry` → `Acquisition`) never call `Model.delete()`, but the
  deletion collector sends `post_delete` for every instance it removes.
- **Paths are captured as strings when the signal fires.** The collector sets
  `instance.pk = None` after sending `post_delete`, so anything computed later
  from the instance would point at `.../None`.
- **Removal runs on commit.** A delete that is rolled back keeps its files.

Storage errors are logged, never raised: the rows are already gone, and a
failed cleanup is what `manage.py storage_audit` exists to find afterwards.
"""

import logging
import posixpath
from typing import Iterable, Optional

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.core.models import Acquisition, Catalog, Entry
from apps.files.storage import get_storage

logger = logging.getLogger(__name__)


def delete_folder(folder: str) -> None:
    """Remove a folder and everything under it from the storage backend."""
    # Imported lazily: `apps.extraction.services` imports from `apps.files`.
    from apps.extraction.services import delete_files

    delete_files(folder)


def _remove(files: list, folders: list) -> None:
    storage = get_storage()
    for name in files:
        try:
            if storage.exists(name):
                storage.delete(name)
        except Exception:
            logger.exception("storage.cleanup_failed path=%s", name)
    for folder in folders:
        try:
            delete_folder(folder)
        except Exception:
            logger.exception("storage.cleanup_failed folder=%s", folder)


def schedule_removal(files: Iterable[Optional[str]] = (), folders: Iterable[Optional[str]] = ()) -> None:
    """Delete these storage paths once the surrounding transaction commits."""
    files = [name for name in files if name]
    folders = [folder for folder in folders if folder]
    if not files and not folders:
        return
    transaction.on_commit(lambda: _remove(files, folders))


def entry_folder(entry: Entry) -> Optional[str]:
    """`catalogs/<catalog slug>/<entry id>` — the directory every file of an entry lives under."""
    catalog = entry._state.fields_cache.get("catalog")
    url_name = getattr(catalog, "url_name", None)
    if url_name is None:
        url_name = Catalog.objects.filter(pk=entry.catalog_id).values_list("url_name", flat=True).first()
    if not url_name or entry.pk is None:
        return None
    return f"catalogs/{url_name}/{entry.pk}"


@receiver(post_delete, sender=Acquisition, dispatch_uid="files.remove_acquisition_files")
def remove_acquisition_files(sender, instance: Acquisition, **kwargs):
    """The book file itself and the IP-018 extraction crops of every run."""
    if instance.storage_backend != Acquisition.StorageBackend.LOCAL or not instance.content:
        # External URLs are pointers, not owned files.
        return
    name = instance.content.name
    schedule_removal(files=[name], folders=[f"{posixpath.dirname(name)}/extraction/{instance.pk}"])


@receiver(post_delete, sender=Entry, dispatch_uid="files.remove_entry_files")
def remove_entry_files(sender, instance: Entry, **kwargs):
    """Cover and thumbnail, then the whole entry directory (old covers, packages, leftovers)."""
    schedule_removal(
        files=[
            instance.image.name if instance.image else None,
            instance.thumbnail.name if instance.thumbnail else None,
        ],
        folders=[entry_folder(instance)],
    )


@receiver(post_delete, sender=Catalog, dispatch_uid="files.remove_catalog_files")
def remove_catalog_files(sender, instance: Catalog, **kwargs):
    """Whatever is left under `catalogs/<slug>/` after its entries went."""
    if instance.url_name:
        schedule_removal(folders=[f"catalogs/{instance.url_name}"])


__all__ = ["delete_folder", "entry_folder", "schedule_removal"]
