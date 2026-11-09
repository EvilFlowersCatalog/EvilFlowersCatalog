"""Backfill / refresh LDAP profiles and notification contacts without a login (IP-025).

    python manage.py sync_ldap_profiles --dry-run          # every LDAP source, report only
    python manage.py sync_ldap_profiles                    # write
    python manage.py sync_ldap_profiles --auth-source stuba.sk --username xdoe

The report never prints an address — only what would change.
"""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.core.models import AuthSource, User
from apps.core.services.ldap_directory import DirectorySyncError, sync_users


class Command(BaseCommand):
    help = "Import profile attributes and notification e-mails of LDAP users from the directory"

    def add_arguments(self, parser):
        parser.add_argument("--auth-source", help="Auth source id or name (default: every active LDAP source)")
        parser.add_argument("--username", action="append", help="Only this user (repeatable)")
        parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
        parser.add_argument("--verbose-users", action="store_true", help="Print one line per user")

    def handle(self, *args, **options):
        sources = AuthSource.objects.filter(driver=AuthSource.Driver.LDAP, is_active=True)
        if options["auth_source"]:
            value = options["auth_source"]
            lookup = Q(name=value)
            try:
                lookup |= Q(pk=uuid.UUID(value))
            except ValueError:
                pass
            sources = sources.filter(lookup)
        if not sources.exists():
            raise CommandError("No active LDAP auth source matches.")

        failed = False
        for source in sources.order_by("name"):
            users = None
            if options["username"]:
                users = User.objects.filter(auth_source=source, username__in=[u.lower() for u in options["username"]])
            try:
                report = sync_users(source, users, dry_run=options["dry_run"])
            except DirectorySyncError as error:
                self.stderr.write(self.style.ERROR(f"{source.name}: {error}"))
                failed = True
                continue

            prefix = "[dry-run] " if options["dry_run"] else ""
            summary = " ".join(f"{k}={v}" for k, v in report.summary.items())
            self.stdout.write(f"{prefix}{source.name} (bind={report.bind}): {summary}")

            for result in report.results:
                if not (options["verbose_users"] or result.error or not result.found or result.changed):
                    continue
                if result.error:
                    line = f"error: {result.error}"
                elif not result.found:
                    line = "not in directory"
                else:
                    parts = [f"{name} updated" for name in result.profile]
                    parts += [
                        f"{c.contact_type} {c.action}{' (primary)' if c.is_primary else ''}" for c in result.contacts
                    ]
                    line = ", ".join(parts) or "unchanged"
                self.stdout.write(f"  {result.username}: {line}")

        if failed:
            raise CommandError("At least one auth source could not be synced.")
