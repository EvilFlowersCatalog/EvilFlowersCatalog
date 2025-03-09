import subprocess
import os
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.management.storage import BackupDestination
from apps.core.models import Catalog


class Command(BaseCommand):
    help = "Primitive EvilFlowers backup mechanism which will backup all catalogs, storage and PostgreSQL database"

    def add_arguments(self, parser):
        parser.add_argument(
            "-d",
            "--destination",
            type=str,
            help="Backup storage path (e.g. /path/to/backup or s3://bucket/folder)",
            default=Path.cwd(),
        )
        parser.add_argument("--pg-dump-binary", type=str, help="Path to pg_dump to use", default="pg_dump")

    def handle(self, *args, **options):
        started_at = timezone.now()
        self.stdout.write(f"Started: {started_at.isoformat()}")

        backup_dest = BackupDestination(f"{options["destination"]}/database.backup")
        strategy = backup_dest.get_strategy()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".backup") as tmp:
            tmp_file = tmp.name
            self.stdout.write(f"Creating temporary file {tmp_file}")

        # Build the pg_dump command.
        pg_dump_command = [
            options["pg_dump_binary"],
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            "--no-comments",
            "-f",
            tmp_file,
        ]

        self.stdout.write("Starting pg_dump backup...")

        try:
            result = subprocess.run(pg_dump_command, capture_output=True, text=True)
            if result.returncode != 0:
                self.stderr.write(f"pg_dump failed: {result.stderr}")
                os.remove(tmp_file)
                return
        except Exception as e:
            self.stderr.write(f"Exception during pg_dump: {e}")
            os.remove(tmp_file)
            return

        self.stdout.write("Database backup created.")

        try:
            final_destination = strategy.move(tmp_file)
            self.stdout.write(self.style.SUCCESS(f"Database backup saved to {final_destination}"))
        except Exception as e:
            self.stderr.write(f"Error saving backup: {e}")
            return

        self.stdout.write(f"Starting dump_catalog for {Catalog.objects.count()} catalogs...")

        for catalog in Catalog.objects.all():
            self.stdout.write(f"Processing {catalog.url_name}")
            try:
                call_command(
                    "dump_catalog",
                    id=str(catalog.pk),
                    output=f"{options["destination"]}/{catalog.url_name}.tar.xz",
                    compression="xz",
                    verbosity=0,
                )
            except Exception as e:
                self.stderr.write(f"Error dumping catalog {catalog.pk}: {e}")

        self.stdout.write(self.style.SUCCESS("All catalogs dumped successfully."))

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
