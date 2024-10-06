import subprocess
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Primitive EvilFlowers backup mechanism which will backup all catalogs, storage and PostgreSQL database"

    def add_arguments(self, parser):
        parser.add_argument(
            "storage", type=Path, help="Backup storage path", default=settings.EVILFLOWERS_BACKUP_STORAGE
        )

    def handle(self, *args, **kwargs):
        # TODO: not ready yet
        backup_storage = kwargs["backup_storage"]

        # Ensure backup path exists
        if not os.path.exists(backup_storage):
            os.makedirs(backup_storage)

        # Define the backup file path
        backup_file_path = os.path.join(backup_storage, "db_backup.dump")

        # Define the pg_dump command with custom format and compression level 9
        pg_dump_command = [
            "pg_dump",
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            "--no-comments",
            "--file={}".format(backup_file_path),
        ]

        try:
            # Execute the pg_dump command using subprocess
            result = subprocess.run(pg_dump_command, capture_output=True, text=True)

            # Check if the backup process succeeded
            if result.returncode == 0:
                self.stdout.write(self.style.SUCCESS("Database backup completed successfully."))
            else:
                # Log the error and output in case of failure
                self.stderr.write(self.style.ERROR("Backup failed. Check logs for details."))
                self.stderr.write(self.style.ERROR(result.stderr))

        except Exception as e:
            # Catch any other exceptions that might occur
            self.stderr.write(self.style.ERROR("Exception occurred during backup: {}".format(str(e))))
