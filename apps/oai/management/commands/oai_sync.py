import json
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.oai.sync import OaiRecordSync


class Command(BaseCommand):
    help = (
        "Bring oai_records in step with the catalog (IP-033): --all is the nightly reconcile (the initial "
        "backfill), --entry syncs one entry. --dry-run counts what would change without writing."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--all", action="store_true", help="Every harvestable entry and every live record.")
        target.add_argument("--entry", action="append", default=[], metavar="UUID", help="An entry id (repeatable).")
        parser.add_argument("--dry-run", action="store_true", help="Count changes, write nothing.")

    def handle(self, *args, **options):
        sync = OaiRecordSync(dry_run=options["dry_run"])
        if options["all"]:
            result = sync.reconcile()
        else:
            try:
                entry_ids = [str(uuid.UUID(value)) for value in options["entry"]]
            except ValueError as error:
                raise CommandError(f"Not an entry id: {error}")
            result = sync.sync(entry_ids)
        prefix = "dry run: " if options["dry_run"] else ""
        self.stdout.write(f"{prefix}{json.dumps(result.as_dict())}")
