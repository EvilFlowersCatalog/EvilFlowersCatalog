import shutil
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management import BaseCommand
from django.utils import timezone

from apps.core.models import Catalog


class Command(BaseCommand):
    help = "Remove catalog from database and filesystem"

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
            self.stderr.write(self.style.ERROR(f"Catalog {options['catalog']} does not exists!"))
            return

        # Related files
        if settings.EVILFLOWERS_STORAGE_DRIVER == "apps.files.storage.filesystem.FileSystemStorage":
            catalog_path = Path(f"{settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR}/catalogs/{catalog.url_name}")
            if catalog_path.exists():
                shutil.rmtree(catalog_path)
        else:
            self.stderr.write(
                self.style.WARNING(
                    "Automatic file removal is currently supported only for FileSystemStorage. "
                    "If you are using different storage, you have to remove orphans manually."
                )
            )

        catalog.delete()

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
