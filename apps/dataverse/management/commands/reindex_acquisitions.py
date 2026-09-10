"""
Operator command to re-publish text-service indexing jobs.

IP-008 Phase 6 F2: when the text-service worker queue is recreated or
the search index needs to be rebuilt, this command walks the eligible
acquisitions and re-enqueues the indexing task. The task_id is
deterministic (`text:index:{entry_id}`) so Celery coalesces duplicate
re-enqueues — running this twice is safe.

Examples:
    python manage.py reindex_acquisitions
    python manage.py reindex_acquisitions --catalog stu
    python manage.py reindex_acquisitions --since 2026-05-01
    python manage.py reindex_acquisitions --catalog stu --since 2026-05-01
"""

import logging
from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.utils.timezone import make_aware

from apps.core.models import Acquisition
from apps.dataverse.services.text_publish import TextServiceClient

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-enqueue text-service indexing for matching acquisitions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--catalog",
            dest="catalog_url_name",
            required=False,
            help="Restrict to acquisitions in the given catalog (url_name).",
        )
        parser.add_argument(
            "--since",
            dest="since",
            required=False,
            help="Restrict to acquisitions touched on or after this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="Print what would be enqueued without publishing tasks.",
        )

    def handle(self, *args, **options):
        catalog_url_name = options.get("catalog_url_name")
        since_raw = options.get("since")
        dry_run = options.get("dry_run", False)

        # Only acquisitions with locally stored content are indexable —
        # external_url rows (Dataverse pointers) are not fetched by us.
        qs = Acquisition.objects.filter(
            storage_backend=Acquisition.StorageBackend.LOCAL,
            mime=Acquisition.AcquisitionMIME.PDF,
            content__isnull=False,
        ).exclude(content="")

        if catalog_url_name:
            qs = qs.filter(entry__catalog__url_name=catalog_url_name)

        if since_raw:
            try:
                since_dt = datetime.strptime(since_raw, "%Y-%m-%d")
            except ValueError:
                self.stderr.write(f"--since must be YYYY-MM-DD, got {since_raw!r}")
                return
            qs = qs.filter(updated_at__gte=make_aware(datetime.combine(since_dt.date(), time.min)))

        total = qs.count()
        self.stdout.write(f"Found {total} acquisitions matching filters")
        if total == 0:
            return

        client = TextServiceClient()
        published = 0
        skipped = 0
        for acquisition in qs.select_related("entry").iterator():
            source = acquisition.content.name
            entry_id = str(acquisition.entry_id)

            if dry_run:
                self.stdout.write(f"  [dry-run] would publish: acquisition={acquisition.pk} source={source}")
                continue

            result = client.process_acquisition(source, entry_id)
            if result is None:
                skipped += 1
                logger.warning("reindex_acquisitions skipped acquisition=%s (publish failed)", acquisition.pk)
                continue
            published += 1

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete — {total} candidates"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Published {published} / {total} (skipped {skipped})"))
