"""
Operator command to store the content hashes that version cover URLs (#128).

`Entry.image_hash` / `Entry.thumbnail_hash` are computed by `Entry.save()` whenever a
cover or thumbnail file changes. Rows written before the columns existed have NULL,
and their URLs stay unversioned (`public, no-cache`) until this command fills them in.

Hashes are written with `QuerySet.update()`, so no post-save receivers run (no search
reindex, no LCP trigger). The touched entries then get a fresh `touched_at`, so OPDS
feeds stop answering 304 with pages that still carry the unversioned URLs.

Safe to re-run: only NULL hashes are computed unless `--force` is given. A file that is
missing on storage keeps NULL (and an unversioned URL).

Examples:
    python manage.py backfill_cover_hashes
    python manage.py backfill_cover_hashes --catalog stu
    python manage.py backfill_cover_hashes --dry-run
    python manage.py backfill_cover_hashes --force
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.core.models import Entry
from apps.core.models.entry import COVER_HASH_FIELDS, cover_content_hash
from apps.core.touch import touch_entries
from evil_flowers_catalog.tracing import TracedCommandMixin


class Command(TracedCommandMixin, BaseCommand):
    help = "Store content hashes of entry covers and thumbnails (versioned cover URLs, #128)."

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
            help="Report what would be hashed without writing anything.",
        )
        parser.add_argument(
            "--force",
            dest="force",
            action="store_true",
            help="Recompute hashes that are already stored.",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        force = options.get("force", False)

        candidates = Q()
        for field, hash_field in COVER_HASH_FIELDS:
            has_file = Q(**{f"{field}__isnull": False}) & ~Q(**{field: ""})
            candidates |= has_file if force else has_file & Q(**{f"{hash_field}__isnull": True})

        qs = Entry.objects.filter(candidates)
        if options.get("catalog_url_name"):
            qs = qs.filter(catalog__url_name=options["catalog_url_name"])

        total = qs.count()
        self.stdout.write(f"Considering {total} entries")

        hashed, missing, changed = 0, 0, []
        for entry in qs.only("pk", "catalog_id", *(name for pair in COVER_HASH_FIELDS for name in pair)).iterator():
            updates = {}
            for field, hash_field in COVER_HASH_FIELDS:
                file = getattr(entry, field)
                if not file or (getattr(entry, hash_field) and not force):
                    continue
                content_hash = cover_content_hash(file)
                if content_hash is None:
                    missing += 1
                    self.stdout.write(f"  entry {entry.pk}: {field} {file.name} not readable on storage")
                    continue
                hashed += 1
                if content_hash != getattr(entry, hash_field):
                    updates[hash_field] = content_hash

            if not updates:
                continue
            changed.append(entry.pk)
            if dry_run:
                self.stdout.write(f"  [dry-run] entry {entry.pk}: would set {', '.join(sorted(updates))}")
            else:
                Entry.objects.filter(pk=entry.pk).update(**updates)

        if not dry_run:
            for start in range(0, len(changed), 500):
                touch_entries(changed[start : start + 500])

        prefix = "Dry run: " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}hashed {hashed} files, {len(changed)} entries updated, {missing} files missing on storage"
            )
        )
