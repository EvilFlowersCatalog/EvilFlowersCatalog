import tarfile
import tempfile
import shutil
import os

from django.conf import settings
from django.core.management import BaseCommand
from django.core.serializers import deserialize
from django.utils import timezone

from apps.core.models import User, Catalog


class Command(BaseCommand):
    help = "Import catalog from TAR with related metadata"

    def add_arguments(self, parser):
        parser.add_argument("--skip-files", action="store_true", help="Skip static files")
        parser.add_argument("--input", type=str, required=True, help="Path to the tar archive")

    def handle(self, *args, **options):
        started_at = timezone.now()
        self.stdout.write(f"Started: {started_at.isoformat()}")

        tar_file_path = options["input"]

        # Use tempfile for handling possible decompression and storage
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                with tarfile.open(tar_file_path, "r") as tar:
                    self.stdout.write(f"Opened TAR file: {tar_file_path}")

                    # Extract entities.json containing serialized data
                    entities_file = tar.extractfile("entities.json")
                    if not entities_file:
                        self.stderr.write(f"entities.json not found in the archive!")
                        return

                    entities_data = entities_file.read().decode("utf-8")
                    deserialized_objects = list(deserialize("json", entities_data))
                    catalog_names = []

                    self.stdout.write(f"Deserialized {len(deserialized_objects)} objects from entities.json")

                    # Iterate over deserialized data and save objects
                    for obj in deserialized_objects:
                        if isinstance(obj.object, Catalog):
                            catalog_names.append(obj.object.url_name)

                        if hasattr(obj.object, "creator_id"):
                            obj.object.creator_id = User.objects.filter(is_superuser=True).first().pk
                        obj.save()
                        self.stdout.write(f"Saved {obj.object.__class__.__name__}: {obj.object.pk}")

                    # Check for storage directory and extract it
                    if not options["skip_files"]:
                        if settings.EVILFLOWERS_STORAGE_DRIVER == "apps.files.storage.filesystem.FileSystemStorage":
                            self.stdout.write(f"Extracting 'storage' directory to temporary folder")
                            tar.extractall(path=temp_dir)

                            extracted_storage_path = os.path.join(temp_dir, "storage")
                            destination_storage_path = settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR

                            for catalog_name in catalog_names:
                                source = os.path.join(extracted_storage_path, f"catalogs/{catalog_name}")
                                destination = os.path.join(destination_storage_path, f"catalogs/{catalog_name}")

                                self.stdout.write(f"Moving '{source}' to '{destination}'")

                                shutil.copytree(
                                    source,
                                    destination,
                                )

            except (tarfile.TarError, FileNotFoundError) as e:
                self.stderr.write(f"Error processing TAR file: {e}")
                return

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
