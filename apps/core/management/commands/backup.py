import subprocess
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.management.storage import BackupDestination
from apps.core.models import Catalog
from evil_flowers_catalog.tracing import TracedCommandMixin


def pg_dump_env() -> dict:
    """The environment for pg_dump: the process's own, with the direct database host when set.

    Behind PgBouncer (#103) PGHOST/PGPORT point at the pooler; pg_dump is not reliable through
    it in transaction mode, so EVILFLOWERS_BACKUP_PGHOST/PGPORT can name the server itself.
    """
    env = os.environ.copy()
    if settings.EVILFLOWERS_BACKUP_PGHOST:
        env["PGHOST"] = settings.EVILFLOWERS_BACKUP_PGHOST
    if settings.EVILFLOWERS_BACKUP_PGPORT:
        env["PGPORT"] = str(settings.EVILFLOWERS_BACKUP_PGPORT)
    return env


class Command(TracedCommandMixin, BaseCommand):
    help = "Primitive EvilFlowers backup mechanism which will backup all catalogs, storage and PostgreSQL database"

    def add_arguments(self, parser):
        parser.add_argument(
            "-d",
            "--destination",
            type=str,
            help="Backup storage path (e.g. /path/to/backup or s3://bucket/folder)",
            default=Path.cwd(),
        )

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
            settings.EVILFLOWERS_BACKUP_PGDUMP_BIN,
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            "--no-comments",
            "-f",
            tmp_file,
        ]

        self.stdout.write("Starting pg_dump backup...")

        # A failure raises, so the `backup` Celery task fails too instead of reporting success.
        try:
            result = subprocess.run(pg_dump_command, capture_output=True, text=True, env=pg_dump_env())
        except Exception as e:
            os.remove(tmp_file)
            raise CommandError(f"Exception during pg_dump: {e}") from e
        if result.returncode != 0:
            os.remove(tmp_file)
            raise CommandError(f"pg_dump failed: {result.stderr}")

        self.stdout.write("Database backup created.")

        try:
            final_destination = strategy.move(tmp_file)
        except Exception as e:
            raise CommandError(f"Error saving backup: {e}") from e
        self.stdout.write(self.style.SUCCESS(f"Database backup saved to {final_destination}"))

        self.stdout.write(f"Starting dump_catalog for {Catalog.objects.count()} catalogs...")

        failed = []
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
                failed.append(catalog.url_name)

        if failed:
            raise CommandError(f"Dumping {len(failed)} catalog(s) failed: {', '.join(failed)}")
        self.stdout.write(self.style.SUCCESS("All catalogs dumped successfully."))

        self.stdout.write(f"Finished: {timezone.now().isoformat()}")
        self.stdout.write(f"Duration: {timezone.now() - started_at}")
