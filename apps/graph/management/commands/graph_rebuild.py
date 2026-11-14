"""Drop and re-project the `library` graph (IP-030 Phase 3b), or only report its node counts.

python manage.py graph_rebuild           # drop, create, project every active run and summary
python manage.py graph_rebuild --counts  # nodes per label, to compare with the tables
"""

from django.core.management.base import BaseCommand, CommandError

from apps.graph import age, projection


class Command(BaseCommand):
    help = "Rebuild the Apache AGE library graph from the relational tables."

    def add_arguments(self, parser):
        parser.add_argument("--counts", action="store_true", help="Only print node counts.")

    def handle(self, *args, **options):
        if options["counts"]:
            self.stdout.write(str(projection.counts()))
            return
        if not age.create_graph():
            raise CommandError("Apache AGE is not installed in this database (CREATE EXTENSION age as a superuser).")
        totals = projection.rebuild()
        self.stdout.write(self.style.SUCCESS(f"Projected {totals}; nodes {projection.counts()}"))
