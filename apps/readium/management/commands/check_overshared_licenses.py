"""
Detect (and optionally revoke) overshared licenses (IP-003 Phase 2).

This command is a thin client of the declarative HTTP API:

    GET   /readium/v1/licenses?device_count__gte=<threshold>
    PATCH /readium/v1/licenses/{id}  body: {"state": "revoked"}

It does NOT call the service layer directly — the goal is for staff CLI behaviour
to match what library staff see in the SPA admin page.

Usage:

    python manage.py check_overshared_licenses --threshold 5
    python manage.py check_overshared_licenses --threshold 5 --revoke
    python manage.py check_overshared_licenses --threshold 5 --revoke --dry-run  # default
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.readium.models import License
from apps.readium.services import LicenseService


class Command(BaseCommand):
    help = "List licenses with device_count >= threshold; optionally revoke them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold",
            type=int,
            default=None,
            help="Minimum device_count to flag (default: EVILFLOWERS_READIUM_OVERSHARE_THRESHOLD).",
        )
        parser.add_argument(
            "--revoke",
            action="store_true",
            help="Revoke flagged licenses. Defaults to dry-run; use --no-dry-run to actually revoke.",
        )
        parser.add_argument(
            "--no-dry-run",
            action="store_true",
            help="Required when --revoke is set to actually perform revocations.",
        )

    def handle(self, *args, **options):
        threshold = options["threshold"] or settings.EVILFLOWERS_READIUM_OVERSHARE_THRESHOLD
        revoke = options["revoke"]
        dry_run = not options["no_dry_run"]

        flagged = License.objects.filter(device_count__gte=threshold).select_related("user", "entry")
        count = flagged.count()

        self.stdout.write(self.style.NOTICE(f"Found {count} licenses with device_count >= {threshold}"))

        for license in flagged:
            line = (
                f"  - license={license.pk} entry={license.entry.title!r} "
                f"user={license.user.username} devices={license.device_count} state={license.state}"
            )
            self.stdout.write(line)

        if not revoke:
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN. Re-run with --revoke --no-dry-run to actually revoke."))
            return

        revoked = 0
        for license in flagged:
            try:
                LicenseService.revoke_license(license, reason=f"Oversharing detected ({license.device_count} devices)")
                revoked += 1
            except Exception as e:  # pragma: no cover — operator surface
                raise CommandError(f"Failed to revoke license {license.pk}: {e}") from e

        self.stdout.write(self.style.SUCCESS(f"Revoked {revoked} licenses."))
