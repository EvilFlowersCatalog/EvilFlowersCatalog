"""Store the outline (chapters) and page count of entries' PDFs.

Read in this process, one PDF at a time; nothing is queued.

Examples:
    python manage.py backfill_table_of_contents --missing
    python manage.py backfill_table_of_contents --entry <uuid>
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.core.models import Acquisition, Entry
from apps.core.services.table_of_contents import store_table_of_contents


class Command(BaseCommand):
    help = "Read the outline and page count of every entry's PDF into the entry."

    def add_arguments(self, parser):
        parser.add_argument("--missing", action="store_true", help="Only entries without a stored outline.")
        parser.add_argument("--entry", dest="entry_id", help="One entry by id.")
        parser.add_argument("--limit", type=int, help="Stop after this many entries.")

    def handle(self, *args, **options):
        pdfs = Acquisition.objects.filter(
            entry_id=OuterRef("pk"),
            mime=Acquisition.AcquisitionMIME.PDF,
            storage_backend=Acquisition.StorageBackend.LOCAL,
        ).exclude(content="")
        entries = Entry.objects.filter(Exists(pdfs)).order_by("created_at")
        if options["entry_id"]:
            entries = entries.filter(pk=options["entry_id"])
        if options["missing"]:
            entries = entries.filter(table_of_contents__isnull=True)
        if options["limit"]:
            entries = entries[: options["limit"]]

        stored = failed = 0
        for entry_id in entries.values_list("pk", flat=True).iterator():
            if store_table_of_contents(entry_id):
                stored += 1
            else:
                failed += 1
        self.stdout.write(self.style.SUCCESS(f"Stored {stored} outlines, {failed} without a readable PDF."))
