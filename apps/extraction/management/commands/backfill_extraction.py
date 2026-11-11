"""Queue extraction (IP-018) for PDFs already in the catalog.

Without a selector every stored PDF is considered; a run of the current
pipeline version that is queued, running or done is left alone, so running
this twice is safe. Progress is visible in Flower on the analyzer queue.

Examples:
    python manage.py backfill_extraction --missing
    python manage.py backfill_extraction --outdated --rate 30
    python manage.py backfill_extraction --acquisition <uuid> --force
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef

from apps.core.models import Acquisition
from apps.extraction import services
from apps.extraction.models import ExtractionRun
from evil_flowers_catalog import tracing


class Command(tracing.TracedCommandMixin, BaseCommand):
    help = "Queue figure, table, equation and paragraph extraction for existing PDF acquisitions."

    def add_arguments(self, parser):
        parser.add_argument("--missing", action="store_true", help="Acquisitions without an active run.")
        parser.add_argument(
            "--outdated", action="store_true", help="Acquisitions whose active run is of an older pipeline version."
        )
        parser.add_argument("--acquisition", dest="acquisition_id", help="One acquisition by id.")
        parser.add_argument(
            "--force", action="store_true", help="Re-extract even when a run of this version exists or is stuck."
        )
        parser.add_argument("--rate", type=float, default=60.0, help="Acquisitions queued per minute (default 60).")
        parser.add_argument("--limit", type=int, help="Stop after queuing this many.")
        parser.add_argument("--dry-run", action="store_true", help="List what would be queued.")

    def handle(self, *args, **options):
        if not settings.EVILFLOWERS_EXTRACTION_ENABLED:
            raise CommandError("EVILFLOWERS_EXTRACTION_ENABLED is off.")

        version = settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
        acquisitions = (
            Acquisition.objects.filter(
                mime=Acquisition.AcquisitionMIME.PDF, storage_backend=Acquisition.StorageBackend.LOCAL
            )
            .exclude(content="")
            .exclude(content__isnull=True)
            .select_related("entry__catalog")
            .order_by("created_at")
        )
        if options["acquisition_id"]:
            acquisitions = acquisitions.filter(pk=options["acquisition_id"])

        active = ExtractionRun.objects.filter(acquisition=OuterRef("pk"), is_active=True)
        if options["missing"] and options["outdated"]:
            acquisitions = acquisitions.exclude(Exists(active.filter(pipeline_version=version)))
        elif options["missing"]:
            acquisitions = acquisitions.exclude(Exists(active))
        elif options["outdated"]:
            acquisitions = acquisitions.filter(Exists(active.filter(pipeline_version__lt=version)))

        delay = 60.0 / options["rate"] if options["rate"] > 0 else 0
        queued = 0
        for acquisition in acquisitions.iterator():
            if options["limit"] and queued >= options["limit"]:
                break
            if options["dry_run"]:
                self.stdout.write(f"would queue {acquisition.pk} {acquisition.content.name}")
                queued += 1
                continue
            # Each acquisition is a trace of its own, linked to the backfill: the analyzer, image, equation
            # and text tasks it causes run for minutes each, and hundreds of them in one trace cannot be
            # opened (IP-029).
            with tracing.root_span(
                "extraction.backfill.item",
                {tracing.ACQUISITION_ID: acquisition.pk, tracing.ENTRY_ID: acquisition.entry_id},
            ):
                # A run left `queued` or `running` by services that were down is sent again.
                stuck = ExtractionRun.objects.filter(
                    acquisition=acquisition,
                    pipeline_version=version,
                    status__in=[ExtractionRun.Status.QUEUED, ExtractionRun.Status.RUNNING],
                ).exists()
                run = services.queue(acquisition, force=options["force"] or stuck)
            if run is None:
                continue
            queued += 1
            self.stdout.write(f"queued {acquisition.pk} run={run.pk}")
            if delay:
                time.sleep(delay)

        self.stdout.write(
            self.style.SUCCESS(f"{queued} acquisition(s) {'listed' if options['dry_run'] else 'queued'}.")
        )
