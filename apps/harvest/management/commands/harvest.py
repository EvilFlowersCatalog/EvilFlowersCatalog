"""`harvest <slug> [--dry-run] [--full]`: one run of a harvest source, in this process (IP-034).

The same run a `harvest.run` job does, without the job ledger: for operators, first imports and debugging.
Scheduled and portal-started runs go through the job.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.harvest.adapters import HarvestError
from apps.harvest.matching import SourceBusy
from apps.harvest.models import HarvestSource
from apps.harvest.runner import run_source


class Command(BaseCommand):
    help = "Harvest one source now: match its records and link, enrich or create entries by its mode."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="The harvest source's slug.")
        parser.add_argument("--dry-run", action="store_true", help="Match and report; write no entries.")
        parser.add_argument("--full", action="store_true", help="Ignore the cursor and unchanged-record skipping.")

    def handle(self, *args, **options):
        source = HarvestSource.objects.select_related("target_catalog", "creator").filter(slug=options["slug"]).first()
        if source is None:
            raise CommandError(f"No harvest source '{options['slug']}'.")
        if not source.enabled and not options["dry_run"]:
            raise CommandError(f"Source '{source.slug}' is disabled; enable it, or use --dry-run.")

        def progress(counters):
            if options["verbosity"] >= 2:
                self.stdout.write(" ".join(f"{key}={value}" for key, value in counters.items()))

        try:
            report = run_source(source, dry_run=options["dry_run"], full=options["full"], progress=progress)
        except (HarvestError, SourceBusy) as error:
            raise CommandError(str(error))
        self.stdout.write(json.dumps(report, indent=2))
