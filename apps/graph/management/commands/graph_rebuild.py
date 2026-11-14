"""Drop and re-project the `library` graph (IP-030 Phase 3b), or only report its node and edge counts.

python manage.py graph_rebuild           # drop, create, project every active run and summary
python manage.py graph_rebuild --counts  # nodes and edges per label (SIMILAR_TO, MENTIONS ...), to compare with the tables

SIMILAR_TO is projected from `extraction_section_similarity`: run `link_extraction --steps similarity` first when
that table is empty (#151).
"""

from django.core.management.base import BaseCommand, CommandError

from apps.graph import age, projection


class Command(BaseCommand):
    help = "Rebuild the Apache AGE library graph from the relational tables."

    def add_arguments(self, parser):
        parser.add_argument("--counts", action="store_true", help="Only print node and edge counts.")

    def handle(self, *args, **options):
        if options["counts"]:
            self.stdout.write(str(projection.counts()))
            return
        try:
            if not age.create_graph():
                raise CommandError(
                    "Apache AGE is not installed in this database (CREATE EXTENSION age as a superuser)."
                )
            totals = projection.rebuild()
        except CommandError:
            raise
        except Exception as error:
            hint = age.permission_hint(error)
            if hint is None:
                raise
            raise CommandError(f"{hint} ({error})") from error
        self.stdout.write(self.style.SUCCESS(f"Projected {totals}; nodes {projection.counts()}"))
