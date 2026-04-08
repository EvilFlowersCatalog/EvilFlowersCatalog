from django.core.management.base import BaseCommand, CommandError

from apps.core.models import User
from apps.notifications.models import NotificationContact
from apps.notifications.services import NotificationService


class Command(BaseCommand):
    help = "Send a test notification to a user"

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username of the recipient")
        parser.add_argument(
            "--type",
            type=str,
            default="license_created",
            help="Notification type to send (default: license_created)",
        )

    def handle(self, *args, **options):
        username = options["username"]
        notification_type = options["type"]

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f'User "{username}" not found')

        email = NotificationService.resolve_recipient_email(user)
        if not email:
            contacts = NotificationContact.objects.filter(user=user)
            if not contacts.exists():
                raise CommandError(f'User "{username}" has no notification contacts. Create one first.')
            raise CommandError(f'User "{username}" has no primary email contact.')

        self.stdout.write(f"Sending {notification_type} notification to {email}...")

        context = {
            "user_name": user.full_name,
            "entry_title": "Test Publication Title",
            "entry_author": "Test Author",
            "starts_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2026-01-15T00:00:00+00:00",
            "passphrase_hint": "test hint",
            "download_url": "https://example.com/test-download",
            "download_expires_hours": 72,
        }

        log = NotificationService.send(
            notification_type=notification_type,
            recipient_user=user,
            context=context,
        )

        if log.status == log.Status.SENT:
            self.stdout.write(self.style.SUCCESS(f"Notification sent successfully (log id: {log.pk})"))
        else:
            self.stdout.write(self.style.ERROR(f"Notification failed: {log.error_message}"))
