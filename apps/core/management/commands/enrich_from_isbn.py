"""Fill authors, publisher and year of entries that have an ISBN but lack them (#202).

Looks the ISBN up with `EVILFLOWERS_ISBN_METADATA_SERVICES` (free services) and fills empty fields only; a value
a librarian entered is never replaced, and authors are added only to an entry without any. Waits
`EVILFLOWERS_ISBN_METADATA_DELAY` seconds between lookups.

Examples:
    python manage.py enrich_from_isbn --catalog stu --dry-run
    python manage.py enrich_from_isbn --entry <uuid>
    python manage.py enrich_from_isbn --limit 50
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef, Q

from apps.core.models import Entry, EntryAuthor
from apps.core.services import isbn_metadata
from evil_flowers_catalog.tracing import TracedCommandMixin


class Command(TracedCommandMixin, BaseCommand):
    help = "Fill missing authors, publisher and year of entries from their ISBN."

    def add_arguments(self, parser):
        parser.add_argument("--catalog", help="Only entries of this catalog (url_name or id).")
        parser.add_argument("--entry", dest="entry_id", help="One entry by id.")
        parser.add_argument("--limit", type=int, help="Stop after this many lookups.")
        parser.add_argument("--dry-run", action="store_true", help="Print what would change; write nothing.")
        parser.add_argument(
            "--delay",
            type=float,
            default=None,
            help="Seconds between lookups (default EVILFLOWERS_ISBN_METADATA_DELAY).",
        )

    def handle(self, *args, **options):
        delay = settings.EVILFLOWERS_ISBN_METADATA_DELAY if options["delay"] is None else options["delay"]
        entries = (
            Entry.objects.exclude(Q(identifiers__isbn__isnull=True) | Q(identifiers__isbn=""))
            .filter(
                ~Exists(EntryAuthor.objects.filter(entry_id=OuterRef("pk")))
                | Q(publisher__isnull=True)
                | Q(publisher="")
                | Q(published_at__isnull=True)
            )
            .select_related("catalog")
            .order_by("created_at")
        )
        if options["catalog"]:
            catalog = options["catalog"]
            condition = Q(catalog__url_name=catalog)
            if _uuid(catalog):
                condition |= Q(catalog__pk=catalog)
            entries = entries.filter(condition)
        if options["entry_id"]:
            entries = entries.filter(pk=options["entry_id"])
        if options["limit"]:
            entries = entries[: options["limit"]]

        filled = unchanged = not_found = 0
        for index, entry in enumerate(entries.iterator()):
            if index and delay > 0:
                time.sleep(delay)
            metadata = isbn_metadata.lookup(entry.identifiers.get("isbn"))
            if metadata is None:
                not_found += 1
                self.stdout.write(f"{entry.pk} not found: {entry.identifiers.get('isbn')}")
                continue
            found = isbn_metadata.enrich(entry, metadata, dry_run=options["dry_run"])
            if not found:
                unchanged += 1
                continue
            filled += 1
            verb = "would fill" if options["dry_run"] else "filled"
            self.stdout.write(f"{entry.pk} {verb} ({metadata.service}): {isbn_metadata.describe(found)}")

        prefix = "Dry run: " if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{filled} entries filled, {unchanged} with nothing new, {not_found} not found by any service."
            )
        )


def _uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True
