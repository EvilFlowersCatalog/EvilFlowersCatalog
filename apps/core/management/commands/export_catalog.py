import tarfile
from io import BytesIO
from uuid import UUID

from django.conf import settings
from django.core.management import BaseCommand
from django.core.serializers import serialize
from django.utils import timezone

from apps.core.models import Catalog, Entry, Acquisition, Feed, Category, Price, Author


class Command(BaseCommand):
    help = "Export catalog as TAR with related metadata"

    def add_arguments(self, parser):
        parser.add_argument("--catalog", type=UUID, default=None)
        parser.add_argument("--output", type=str, default=None, help="Path to save the tar archive")

    @classmethod
    def _add_file_to_tar(cls, tar: tarfile.TarFile, filename: str, content: BytesIO) -> None:
        tarinfo = tarfile.TarInfo(name=filename)
        tarinfo.size = len(content.getvalue())

        tar.addfile(tarinfo, content)

    def handle(self, *args, **options):
        started_at = timezone.now()
        self.stdout.write(f"Started: {started_at.isoformat()}")

        try:
            catalog = Catalog.objects.get(pk=options["catalog"])
        except Catalog.DoesNotExist:
            self.stderr.write(f"Catalog {options['catalog']} does not exists!")
            return

        self.stdout.write(f"Preparing to backup catalog {catalog.title} ({catalog.pk})")

        with tarfile.open(options["output"] or f"{catalog.url_name}.tar", "w") as tar:
            # Catalogs
            self._add_file_to_tar(
                tar,
                "entities.json",
                BytesIO(
                    serialize(
                        "json",
                        [
                            *Catalog.objects.filter(pk=catalog.pk),
                            *Category.objects.filter(catalog=catalog),
                            *Author.objects.filter(catalog=catalog),
                            *Entry.objects.filter(catalog=catalog),
                            *Acquisition.objects.filter(entry__catalog=catalog),
                            *Price.objects.filter(acquisition__entry__catalog=catalog),
                            *Feed.objects.filter(catalog=catalog),
                        ],
                    ).encode("utf-8")
                ),
            )

            # Related files
            if settings.EVILFLOWERS_STORAGE_DRIVER == "apps.files.storage.filesystem.FileSystemStorage":
                tar.add(
                    f"{settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR}/catalogs/{catalog.url_name}",
                    f"catalogs/{catalog.url_name}",
                )

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
