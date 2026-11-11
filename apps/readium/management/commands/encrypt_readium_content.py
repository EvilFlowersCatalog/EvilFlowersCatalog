"""
Management command to initialize encrypted content for DRM-enabled entries.

This command finds all entries with readium_enabled=True that don't have
encrypted content yet, and triggers the encryption process for them.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.core.models import Entry, Acquisition
from apps.readium.services import ContentEncryptionService
from evil_flowers_catalog import tracing

PROCESSED, SKIPPED, ERROR = "processed", "skipped", "error"


class Command(tracing.TracedCommandMixin, BaseCommand):
    help = "Initialize encrypted content for DRM-enabled entries without EncryptedContent"

    def add_arguments(self, parser):
        parser.add_argument(
            "--catalog",
            type=str,
            help="Filter by catalog UUID",
        )
        parser.add_argument(
            "--entry",
            type=str,
            help="Process specific entry UUID",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without actually doing it",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-queue encryptions stuck in pending or failed (completed/registered need --reencrypt)",
        )
        parser.add_argument(
            "--reencrypt",
            action="store_true",
            help=(
                "Re-encrypt completed/registered content under the same content id (implies --force). "
                "Entries with active loans are skipped unless --break-active-loans is given"
            ),
        )
        parser.add_argument(
            "--break-active-loans",
            action="store_true",
            help="With --reencrypt: also re-encrypt entries with active loans (their readers lose access)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Stop after queueing this many jobs",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting encrypted content initialization..."))

        dry_run = options.get("dry_run", False)
        self.dry_run = dry_run
        self.force = options.get("force", False)
        self.reencrypt = options.get("reencrypt", False)
        self.break_active_loans = options.get("break_active_loans", False)
        limit = options.get("limit")
        catalog_id = options.get("catalog")
        entry_id = options.get("entry")

        # Build query for readium-enabled entries
        entries_query = Entry.objects.filter(config__readium_enabled=True)

        # Apply filters
        if catalog_id:
            entries_query = entries_query.filter(catalog_id=catalog_id)
            self.stdout.write(f"Filtering by catalog: {catalog_id}")

        if entry_id:
            entries_query = entries_query.filter(pk=entry_id)
            self.stdout.write(f"Processing single entry: {entry_id}")

        entries = entries_query.select_related("catalog")
        total_entries = entries.count()

        self.stdout.write(f"Found {total_entries} readium-enabled entries\n")

        counts = {PROCESSED: 0, SKIPPED: 0, ERROR: 0}

        for entry in entries:
            # Every queued job makes its title unlendable until the worker
            # reports back, so large runs go in waves.
            if limit and counts[PROCESSED] >= limit:
                self.stdout.write(self.style.WARNING(f"Reached --limit {limit}, stopping"))
                break

            self.stdout.write(f"Processing: {entry.title} ({entry.pk})")

            # Get EPUB and PDF acquisitions (both supported for LCP)
            acquisitions = entry.acquisitions.filter(Q(mime="application/epub+zip") | Q(mime="application/pdf"))

            if not acquisitions.exists():
                self.stdout.write(self.style.WARNING("No EPUB/PDF acquisition found, skipping"))
                counts[SKIPPED] += 1
                continue

            # One trace per entry, linked to the command (IP-029). The encryption task each acquisition
            # queues carries this trace's context, so the worker's `lcpencrypt` span and its result hook
            # land in it (on dev; production's worker image has no SDK yet). lcpencrypt's own `-notify`
            # call stays a separate trace.
            with tracing.root_span(
                "lcp.encrypt.backfill.item", {tracing.ENTRY_ID: entry.pk, tracing.CATALOG_ID: entry.catalog_id}
            ):
                for acquisition in acquisitions:
                    counts[self._process_acquisition(acquisition)] += 1

            self.stdout.write("")  # Empty line between entries

        # Summary
        self.stdout.write("=" * 70)
        self.stdout.write(self.style.SUCCESS("Summary:"))
        self.stdout.write(f"Total entries checked: {total_entries}")
        self.stdout.write(self.style.SUCCESS(f"Processed: {counts[PROCESSED]}"))
        self.stdout.write(self.style.WARNING(f"Skipped: {counts[SKIPPED]}"))
        if counts[ERROR] > 0:
            self.stdout.write(self.style.ERROR(f"Errors: {counts[ERROR]}"))

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No actual changes were made"))

    def _process_acquisition(self, acquisition: Acquisition) -> str:
        # Check if already has encrypted content
        if hasattr(acquisition, "encrypted_content"):
            return self._process_existing(acquisition.encrypted_content)

        # Check if acquisition has content file
        if not acquisition.content:
            self.stdout.write(self.style.WARNING("Acquisition has no content file, skipping"))
            return SKIPPED

        # Trigger encryption
        try:
            if self.dry_run:
                self.stdout.write(
                    self.style.SUCCESS(f"[DRY RUN] Would encrypt: {acquisition.content.name} ({acquisition.mime})")
                )
            else:
                encrypted_content = ContentEncryptionService.encrypt_acquisition(acquisition)
                self.stdout.write(self.style.SUCCESS(f"Encryption queued: {encrypted_content.lcp_content_id}"))
                self.stdout.write(f"Type: {acquisition.mime}, Status: {encrypted_content.status}")
            return PROCESSED
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to trigger encryption: {str(e)}"))
            return ERROR

    def _process_existing(self, encrypted_content) -> str:
        status = encrypted_content.status

        if self.reencrypt and status not in ContentEncryptionService.REQUEUEABLE_STATUSES:
            active = ContentEncryptionService.active_licenses(encrypted_content).count()
            if active and not self.break_active_loans:
                self.stdout.write(self.style.WARNING(f"{active} active loan(s), skipping re-encryption"))
                return SKIPPED

            if self.dry_run:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[DRY RUN] Would re-encrypt: {encrypted_content.lcp_content_id} "
                        f"({status}, {active} active loan(s))"
                    )
                )
            else:
                ContentEncryptionService.reencrypt(encrypted_content, force=self.break_active_loans)
                self.stdout.write(self.style.SUCCESS(f"Re-encrypting: {encrypted_content.lcp_content_id}"))
            return PROCESSED

        if not (self.force or self.reencrypt) or status not in ContentEncryptionService.REQUEUEABLE_STATUSES:
            self.stdout.write(self.style.WARNING(f"Already has encrypted content (status: {status}), skipping"))
            return SKIPPED

        # `encrypt_acquisition` returns early for an existing row, so a
        # pending/failed job has to be re-sent explicitly.
        if self.dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"[DRY RUN] Would re-queue: {encrypted_content.lcp_content_id} ({status})")
            )
        else:
            ContentEncryptionService.requeue(encrypted_content)
            self.stdout.write(self.style.SUCCESS(f"Re-queued: {encrypted_content.lcp_content_id}"))
        return PROCESSED
