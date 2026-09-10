"""
IP-009 Phase 2 B4: one-shot backfill command.

Existing dev/staging databases carry rows where `state IN (ready,
active)` and `expires_at < now()` — left over before the Celery beat
sweep landed. This command lists them, optionally transitions them,
and lets the operator scope by catalog so a multi-tenant deployment
can clean up one tenant at a time.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.readium.models import License


class Command(BaseCommand):
    help = "Transition naturally-expired licenses (ready/active with expires_at < now) to EXPIRED."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the rows that would be transitioned without writing.",
        )
        parser.add_argument(
            "--catalog",
            type=str,
            default=None,
            help="Restrict to a single catalog by url_name (default: all catalogs).",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        catalog_url_name: str | None = options["catalog"]
        now = timezone.now()

        qs = License.objects.filter(
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            expires_at__lt=now,
        ).select_related("entry__catalog", "user")

        if catalog_url_name:
            qs = qs.filter(entry__catalog__url_name=catalog_url_name)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No expired ready/active licenses found."))
            return

        verb = "Would transition" if dry_run else "Transitioning"
        self.stdout.write(self.style.WARNING(f"{verb} {total} license(s) to EXPIRED:"))

        for license_obj in qs.iterator():
            tag = f"{license_obj.pk} entry={license_obj.entry_id} user={license_obj.user_id}"
            self.stdout.write(f"  - {tag} expired_at={license_obj.expires_at.isoformat()}")
            if dry_run:
                continue
            with transaction.atomic():
                locked = License.objects.select_for_update().get(pk=license_obj.pk)
                if locked.state not in (License.LicenseState.READY, License.LicenseState.ACTIVE):
                    continue
                if locked.expires_at >= timezone.now():
                    continue
                locked.state = License.LicenseState.EXPIRED
                locked.save(update_fields=["state", "updated_at"])

        if dry_run:
            self.stdout.write(self.style.SUCCESS("Dry run complete. Re-run without --dry-run to apply."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Transitioned {total} license(s) to EXPIRED."))
