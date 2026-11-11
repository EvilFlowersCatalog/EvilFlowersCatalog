from uuid import UUID

from django.core.management import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import Catalog
from evil_flowers_catalog.tracing import TracedCommandMixin


class Command(TracedCommandMixin, BaseCommand):
    help = "Remove catalog from database and storage"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=UUID, default=None, help="Catalog UUID")
        parser.add_argument("--name", type=str, default=None, help="Catalog unique url_name")

    def handle(self, *args, **options):
        started_at = timezone.now()
        self.stdout.write(f"Started: {started_at.isoformat()}")

        conditions = {}
        if options.get("id"):
            conditions["pk"] = options["id"]
        elif options.get("name"):
            conditions["url_name"] = options["name"]
        else:
            self.stderr.write(
                self.style.ERROR(
                    f"Catalog 'id' or 'name' (url_name) have to be provided. Use --help for more information"
                )
            )
            return

        try:
            catalog = Catalog.objects.get(**conditions)
        except Catalog.DoesNotExist:
            identifier = options.get("id") or options.get("name")
            self.stderr.write(self.style.ERROR(f"Catalog {identifier} does not exists!"))
            return

        # Stored files (books, covers, extraction crops, LCP packages) are removed by
        # the post_delete receivers in `apps.files.signals` once this commits — on
        # every storage backend, and only if the rows are really gone.
        with transaction.atomic():
            catalog.delete()

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
