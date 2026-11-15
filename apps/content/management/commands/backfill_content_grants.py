"""Write content grants (#175) for every licence and own copy; idempotent (migration 0004 does it once)."""

from django.core.management.base import BaseCommand

from apps.content import grants


class Command(BaseCommand):
    help = "Write content grants for every Readium LCP licence and own copy (idempotent)."

    def handle(self, *args, **options):
        counts = grants.backfill()
        self.stdout.write(
            self.style.SUCCESS(f"Grants for {counts['licenses']} licences and {counts['acquisitions']} own copies.")
        )
