from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management import BaseCommand, CommandError

from apps.api.services.text_service_client import TextServiceClient

_FILESYSTEM_STORAGE = "apps.files.storage.filesystem.FileSystemStorage"
_CATALOGS = "catalogs"
_STORAGE_ROOT = Path("/usr/local/app/private")


class Command(BaseCommand):
    help = (
        "Walk filesystem storage under catalogs/**/*.pdf and enqueue text-service tasks. "
        "Each path must match catalogs/<catalog_slug>/<entry_uuid>/<file>.pdf "
        "(same layout as Acquisition.upload_to_path)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List PDFs that would be enqueued without publishing tasks.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after this many enqueue attempts (after filters).",
        )
        parser.add_argument(
            "--catalog-slug",
            type=str,
            default=None,
            help="Only PDFs under catalogs/<slug>/... (catalog url_name).",
        )
        parser.add_argument(
            "--one-per-entry",
            action="store_true",
            help="At most one task per entry UUID (first path wins, sorted by path).",
        )
        parser.add_argument(
            "--include-encrypted",
            action="store_true",
            help="Also enqueue PDFs under .../encrypted/ (Readium LCP output). Default: skip them.",
        )

    def handle(self, *args, **options):
        if settings.EVILFLOWERS_STORAGE_DRIVER != _FILESYSTEM_STORAGE:
            self.stderr.write(
                self.style.ERROR(
                    "This command only supports FileSystemStorage; got "
                    f"{settings.EVILFLOWERS_STORAGE_DRIVER}."
                )
            )
            return

        root = _STORAGE_ROOT
        catalogs_dir = root / _CATALOGS
        if not catalogs_dir.is_dir():
            raise CommandError(
                f"Expected catalog files at {catalogs_dir}; "
                f"adjust _STORAGE_ROOT in this command if your layout differs."
            )

        slug_filter = options["catalog_slug"]
        seen_entries: set[str] = set()
        client = TextServiceClient()
        enqueued = 0
        skipped = 0
        failed = 0
        attempt = 0
        limit = options["limit"]

        for path in sorted(catalogs_dir.rglob("*.pdf")):
            if limit is not None and attempt >= limit:
                break

            try:
                rel = path.relative_to(root)
            except ValueError:
                skipped += 1
                self.stdout.write(self.style.WARNING(f"skip outside datadir: {path}"))
                continue

            rel_posix = rel.as_posix()
            parts = rel_posix.split("/")
            if len(parts) < 4 or parts[0] != _CATALOGS:
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(f"skip unexpected layout: {rel_posix}")
                )
                continue

            if (
                len(parts) >= 5
                and parts[3] == "encrypted"
                and not options["include_encrypted"]
            ):
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(f"skip encrypted subtree: {rel_posix}")
                )
                continue

            slug = parts[1]
            if slug_filter is not None and slug != slug_filter:
                continue

            try:
                entry_uuid = UUID(parts[2])
            except ValueError:
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(f"skip entry segment not a UUID: {rel_posix}")
                )
                continue

            entry_id = str(entry_uuid)
            if options["one_per_entry"]:
                if entry_id in seen_entries:
                    continue
                seen_entries.add(entry_id)

            attempt += 1

            if options["dry_run"]:
                enqueued += 1
                self.stdout.write(f"would enqueue entry={entry_id} source={rel_posix}")
                continue

            result = client.process_acquisition(rel_posix, entry_id)
            if result:
                enqueued += 1
                self.stdout.write(
                    f"enqueued task_id={result['task_id']} entry={entry_id} source={rel_posix}"
                )
            else:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"failed to enqueue entry={entry_id} source={rel_posix}")
                )

        self.stdout.write(
            self.style.NOTICE(
                f"done: enqueued={enqueued} skipped={skipped} failed={failed}"
            )
        )
