"""Warm the section summaries of popular publications (IP-030 §4).

Summaries are otherwise generated on a reader's first request. One section costs a model call of up to ~2.5K
prompt tokens on CPU (about a minute), so run this off-peak, a few publications at a time.

    python manage.py summarise_sections --entry <uuid>
    python manage.py summarise_sections --popular 10 --sections 20
    python manage.py summarise_sections --entry <uuid> --force
"""

from django.core.management.base import BaseCommand, CommandError

from apps.assistant.sections import summarise_entry
from apps.core.models import Entry
from apps.extraction.models import ExtractionRun


class Command(BaseCommand):
    help = "Generate section summaries (and their graph concepts) for processed publications."

    def add_arguments(self, parser):
        parser.add_argument("--entry", dest="entry_id", help="One publication by id.")
        parser.add_argument("--popular", type=int, help="The N most-read processed publications.")
        parser.add_argument("--sections", type=int, help="At most this many sections per publication.")
        parser.add_argument("--force", action="store_true", help="Generate again even when current.")

    def handle(self, *args, **options):
        if options["entry_id"]:
            entry_ids = [options["entry_id"]]
        elif options["popular"]:
            processed = ExtractionRun.objects.filter(is_active=True).values("acquisition__entry_id")
            entry_ids = list(
                Entry.objects.filter(pk__in=processed)
                .order_by("-popularity")
                .values_list("pk", flat=True)[: options["popular"]]
            )
        else:
            raise CommandError("Pass --entry or --popular.")
        for entry_id in entry_ids:
            report = summarise_entry(entry_id, force=options["force"], limit=options["sections"])
            self.stdout.write(f"{entry_id}: {report}")
