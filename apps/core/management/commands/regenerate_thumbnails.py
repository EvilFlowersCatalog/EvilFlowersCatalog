"""
Operator command to rebuild entry thumbnails from their stored cover images.

Thumbnails used to be encoded in the cover's own format, so PNG covers
produced heavy PNG previews. They are now re-encoded to JPEG (PNG kept only
when the cover carries transparency) via ``build_thumbnail``. Existing rows
keep their old preview until the cover is re-uploaded; this command backfills
them in place.

Safe to re-run: each entry is regenerated from its cover, and the previous
thumbnail file is removed once the new one is written.

Examples:
    python manage.py regenerate_thumbnails
    python manage.py regenerate_thumbnails --catalog stu
    python manage.py regenerate_thumbnails --dry-run
"""

import logging
from io import BytesIO

from PIL import Image
from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from mimetypes import guess_extension

from apps.api.services.entry import build_thumbnail
from apps.core.models import Entry

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Rebuild entry thumbnails from stored covers (re-encoding to JPEG)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--catalog",
            dest="catalog_url_name",
            required=False,
            help="Restrict to entries in the given catalog (url_name).",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="Report what would be regenerated without writing any files.",
        )

    def handle(self, *args, **options):
        catalog_url_name = options.get("catalog_url_name")
        dry_run = options.get("dry_run", False)

        qs = Entry.objects.filter(image__isnull=False).exclude(image="")
        if catalog_url_name:
            qs = qs.filter(catalog__url_name=catalog_url_name)

        total = qs.count()
        self.stdout.write(f"Found {total} entries with a cover image")
        if total == 0:
            return

        regenerated = 0
        skipped = 0
        failed = 0

        for entry in qs.select_related("catalog").iterator():
            # The cover file may be referenced but missing on disk/S3.
            if not entry.image.storage.exists(entry.image.name):
                skipped += 1
                logger.warning("regenerate_thumbnails: cover missing for entry %s", entry.pk)
                continue

            if dry_run:
                self.stdout.write(f"  [dry-run] would regenerate thumbnail for entry {entry.pk}")
                continue

            try:
                with entry.image.open("rb") as fh:
                    source_image = Image.open(BytesIO(fh.read()))

                buffer, thumbnail_mime = build_thumbnail(source_image, settings.EVILFLOWERS_IMAGE_THUMBNAIL)
            except Exception as exc:  # noqa: BLE001 — one bad cover must not abort the whole run
                failed += 1
                logger.warning("regenerate_thumbnails: failed to build thumbnail for entry %s: %s", entry.pk, exc)
                continue

            old_thumbnail_name = entry.thumbnail.name

            entry.thumbnail_mime = thumbnail_mime
            entry.thumbnail.save(
                f"thumbnail{guess_extension(thumbnail_mime)}",
                File(buffer),
                save=False,
            )
            entry.save(update_fields=["thumbnail", "thumbnail_mime"])

            # Remove the previous thumbnail if it landed on a different path
            # (e.g. the old PNG preview when switching a cover to JPEG).
            if old_thumbnail_name and old_thumbnail_name != entry.thumbnail.name:
                entry.thumbnail.storage.delete(old_thumbnail_name)

            regenerated += 1
            if regenerated % 100 == 0:
                self.stdout.write(f"  … {regenerated} regenerated")

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete — {total} candidates"))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Regenerated {regenerated} / {total} (skipped {skipped}, failed {failed})")
            )
