"""
Operator command to rebuild entry thumbnails.

Thumbnails used to be encoded in the cover's own format, so PNG covers produced
heavy PNG previews. They are now re-encoded to JPEG (PNG kept only when the cover
carries transparency) via ``build_thumbnail``. Existing rows keep their old
preview until the cover is re-uploaded; this command backfills them in place.

Entries whose cover file is missing on storage, or which never had a cover, get
a clean generated placeholder (title + author on a muted background) via
``build_placeholder_thumbnail`` — pass ``--no-placeholders`` to skip those.

Safe to re-run: each entry is regenerated from its cover (or placeholder), and
the previous thumbnail file is removed once the new one is written.

Examples:
    python manage.py regenerate_thumbnails
    python manage.py regenerate_thumbnails --catalog stu
    python manage.py regenerate_thumbnails --dry-run
    python manage.py regenerate_thumbnails --no-placeholders
"""

import logging
from io import BytesIO

from PIL import Image
from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from mimetypes import guess_extension

from apps.api.services.entry import build_thumbnail, build_placeholder_thumbnail
from apps.core.models import Entry

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Rebuild entry thumbnails from stored covers, with generated placeholders as a fallback."

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
        parser.add_argument(
            "--no-placeholders",
            dest="no_placeholders",
            action="store_true",
            help="Do not generate placeholders for entries without a usable cover.",
        )

    def _has_cover(self, entry: Entry) -> bool:
        return bool(entry.image and entry.image.name and entry.image.storage.exists(entry.image.name))

    def handle(self, *args, **options):
        catalog_url_name = options.get("catalog_url_name")
        dry_run = options.get("dry_run", False)
        no_placeholders = options.get("no_placeholders", False)

        qs = Entry.objects.all()
        if catalog_url_name:
            qs = qs.filter(catalog__url_name=catalog_url_name)

        total = qs.count()
        self.stdout.write(f"Considering {total} entries")
        if total == 0:
            return

        from_cover = 0
        placeholders = 0
        skipped = 0
        failed = 0

        for entry in qs.select_related("catalog").iterator():
            has_cover = self._has_cover(entry)

            if not has_cover and no_placeholders:
                skipped += 1
                continue

            source = "cover" if has_cover else "placeholder"

            if dry_run:
                self.stdout.write(f"  [dry-run] entry {entry.pk}: would generate {source} thumbnail")
                continue

            try:
                if has_cover:
                    with entry.image.open("rb") as fh:
                        source_image = Image.open(BytesIO(fh.read()))
                    buffer, thumbnail_mime = build_thumbnail(source_image, settings.EVILFLOWERS_IMAGE_THUMBNAIL)
                else:
                    buffer, thumbnail_mime = build_placeholder_thumbnail(
                        entry.title, entry.first_author_name, settings.EVILFLOWERS_IMAGE_THUMBNAIL
                    )
            except Exception as exc:  # noqa: BLE001 — one bad entry must not abort the whole run
                failed += 1
                logger.warning(
                    "regenerate_thumbnails: failed to build %s thumbnail for entry %s: %s", source, entry.pk, exc
                )
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

            if has_cover:
                from_cover += 1
            else:
                placeholders += 1

            done = from_cover + placeholders
            if done % 100 == 0:
                self.stdout.write(f"  … {done} generated")

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete — {total} candidates"))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Generated {from_cover} from covers, {placeholders} placeholders "
                    f"(skipped {skipped}, failed {failed})"
                )
            )
