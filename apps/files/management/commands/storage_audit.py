"""
Compare storage with the database, both ways.

- **Orphans** — directories under `catalogs/` that no row owns any more: a whole
  catalog directory whose slug is gone, or an entry directory whose entry is
  gone. Deletions before `apps.files.signals` existed left these behind.
- **Missing** — files a row points at that storage does not have: an
  acquisition's book, a cover or thumbnail, a registered LCP package. Found on
  elvira-dev after the 2026-11-07 move to k8s: 49 publications advertised a
  download that answered 404.

Dry run by default. `--delete-orphans` removes the orphan directories (never
anything a row still references); missing files can only be reported — the
fix is restoring them from a backup or re-uploading. `--min-age-hours` keeps an
orphan whose newest file is younger than that; the periodic
`apps.tasks.tasks.cleanup_orphans` (#30) always passes it.

    python manage.py storage_audit
    python manage.py storage_audit --catalog openresearchlibrary --format json
    python manage.py storage_audit --delete-orphans --min-age-hours 168
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Iterator, Optional

from django.core.management import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import Acquisition, Catalog, Entry
from apps.files.signals import delete_folder
from apps.files.storage import get_storage
from apps.readium.models import EncryptedContent
from evil_flowers_catalog.tracing import TracedCommandMixin

ROOT = "catalogs"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _newest_modification(storage, folder: str) -> Optional[datetime]:
    """Latest modification time of any file under `folder`, or None when it holds no file."""
    try:
        directories, files = storage.listdir(folder)
    except FileNotFoundError:
        return None
    times = [storage.get_modified_time(f"{folder}/{name}") for name in files]
    times += [_newest_modification(storage, f"{folder}/{name}") for name in directories]
    times = [moment for moment in times if moment is not None]
    return max(times, default=None)


class Command(TracedCommandMixin, BaseCommand):
    help = "Report orphan directories and missing files in storage; optionally delete the orphans"

    def add_arguments(self, parser):
        parser.add_argument("--catalog", help="Limit the audit to one catalog (url_name).")
        parser.add_argument("--delete-orphans", action="store_true", help="Remove orphan directories.")
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument(
            "--skip-missing",
            action="store_true",
            help="Only look for orphans (one storage call per file is slow on S3).",
        )
        parser.add_argument(
            "--min-age-hours",
            type=float,
            default=None,
            help=(
                "Keep an orphan directory holding a file modified within this many hours. Guards against "
                "files of a publication whose row is not committed yet (#30)."
            ),
        )

    def handle(self, *args, **options):
        storage = get_storage()
        catalog_filter: Optional[str] = options.get("catalog")
        if catalog_filter and not Catalog.objects.filter(url_name=catalog_filter).exists():
            raise CommandError(f"Catalog '{catalog_filter}' does not exist.")

        orphans = list(self._orphans(storage, catalog_filter))
        missing = [] if options["skip_missing"] else list(self._missing(storage, catalog_filter))

        min_age_hours = options.get("min_age_hours")
        if min_age_hours is not None:
            cutoff = timezone.now() - timedelta(hours=min_age_hours)
            for orphan in orphans:
                newest = _newest_modification(storage, orphan["path"])
                if newest is not None and newest > cutoff:
                    orphan["recent"] = True

        if options["delete_orphans"]:
            for orphan in orphans:
                if orphan.get("recent"):
                    continue
                delete_folder(orphan["path"])
                orphan["deleted"] = True

        if options["format"] == "json":
            self.stdout.write(json.dumps({"orphans": orphans, "missing": missing}, indent=2, ensure_ascii=False))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"Orphan directories ({len(orphans)})"))
        for orphan in orphans:
            mark = " [deleted]" if orphan.get("deleted") else " [recent, kept]" if orphan.get("recent") else ""
            self.stdout.write(f"  {orphan['kind']:<8} {orphan['path']}{mark}")

        if not options["skip_missing"]:
            self.stdout.write(self.style.MIGRATE_HEADING(f"Missing files ({len(missing)})"))
            for item in missing:
                self.stdout.write(f"  {item['kind']:<11} {item['entry_id']}  {item['path']}  — {item['title'][:60]}")

        if orphans and not options["delete_orphans"]:
            self.stdout.write(
                self.style.WARNING("Dry run. Re-run with --delete-orphans to remove the orphan directories.")
            )

    # Orphans --------------------------------------------------------------------------

    def _orphans(self, storage, catalog_filter: Optional[str]) -> Iterator[dict]:
        catalogs = dict(Catalog.objects.values_list("url_name", "pk"))
        try:
            slugs, _files = storage.listdir(ROOT)
        except FileNotFoundError:
            return

        for slug in sorted(slugs):
            if catalog_filter and slug != catalog_filter:
                continue
            if slug not in catalogs:
                yield {"kind": "catalog", "path": f"{ROOT}/{slug}"}
                continue

            entry_dirs, _files = storage.listdir(f"{ROOT}/{slug}")
            candidates = [name for name in entry_dirs if _is_uuid(name)]
            alive = {
                str(pk)
                for pk in Entry.objects.filter(catalog_id=catalogs[slug], pk__in=candidates).values_list(
                    "pk", flat=True
                )
            }
            for name in sorted(candidates):
                if name not in alive:
                    yield {"kind": "entry", "path": f"{ROOT}/{slug}/{name}"}

    # Missing --------------------------------------------------------------------------

    def _missing(self, storage, catalog_filter: Optional[str]) -> Iterator[dict]:
        entries = Entry.objects.select_related("catalog")
        acquisitions = Acquisition.objects.filter(storage_backend=Acquisition.StorageBackend.LOCAL).select_related(
            "entry"
        )
        packages = EncryptedContent.objects.filter(
            status__in=[EncryptedContent.EncryptionStatus.COMPLETED, EncryptedContent.EncryptionStatus.REGISTERED]
        ).select_related("acquisition__entry")
        if catalog_filter:
            entries = entries.filter(catalog__url_name=catalog_filter)
            acquisitions = acquisitions.filter(entry__catalog__url_name=catalog_filter)
            packages = packages.filter(acquisition__entry__catalog__url_name=catalog_filter)

        for entry in entries.only("pk", "title", "image", "thumbnail", "catalog__url_name").iterator():
            for kind, field in (("cover", entry.image), ("thumbnail", entry.thumbnail)):
                if field and not storage.exists(field.name):
                    yield {"kind": kind, "entry_id": str(entry.pk), "title": entry.title, "path": field.name}

        for acquisition in acquisitions.iterator():
            if acquisition.content and not storage.exists(acquisition.content.name):
                yield {
                    "kind": "acquisition",
                    "entry_id": str(acquisition.entry_id),
                    "title": acquisition.entry.title,
                    "path": acquisition.content.name,
                }

        for package in packages.iterator():
            if package.encrypted_path and not storage.exists(package.encrypted_path):
                yield {
                    "kind": "lcp-package",
                    "entry_id": str(package.acquisition.entry_id),
                    "title": package.acquisition.entry.title,
                    "path": package.encrypted_path,
                }
