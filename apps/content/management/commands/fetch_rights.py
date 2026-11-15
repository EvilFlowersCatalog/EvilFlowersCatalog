"""Fill `entry.rights` from Crossref for entries with a DOI (#177)."""

from django.core.management.base import BaseCommand

from apps.content.tasks import refresh_crossref_rights


class Command(BaseCommand):
    help = (
        "Look the DOIs of entries up in Crossref and store their licence in entry.rights (never over a manual edit)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--entry", action="append", dest="entries", help="Only this entry (repeatable).")
        parser.add_argument("--all", action="store_true", help="Also entries that already have rights.")

    def handle(self, *args, **options):
        counts = refresh_crossref_rights(options["entries"], missing_only=not options["all"])
        self.stdout.write(self.style.SUCCESS(f"Checked {counts['checked']} entries, updated {counts['updated']}."))
