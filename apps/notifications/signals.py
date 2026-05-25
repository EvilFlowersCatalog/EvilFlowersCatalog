from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.readium.models import License


@receiver(post_save, sender=License)
def on_license_created(sender, instance: License, created: bool, **kwargs):
    if not created:
        return

    if not getattr(settings, "EVILFLOWERS_NOTIFICATIONS_ENABLED", False):
        return

    from apps.notifications.services import NotificationService
    from apps.notifications.tasks import send_notification

    download_url = NotificationService.generate_scoped_url(
        user_id=str(instance.user.pk),
        scope="license:read",
        resource_path=f"/readium/v1/licenses/{instance.pk}.lcpl",
    )

    context = {
        "user_name": instance.user.full_name,
        "entry_title": instance.entry.title,
        "entry_author": instance.entry.first_author_name,
        "starts_at": instance.starts_at.isoformat(),
        "expires_at": instance.expires_at.isoformat(),
        "passphrase_hint": instance.passphrase_hint or "",
        "download_url": download_url,
        "download_expires_hours": settings.EVILFLOWERS_NOTIFICATION_SCOPED_TOKEN_TTL_HOURS,
    }

    send_notification.delay(
        notification_type="license_created",
        recipient_user_id=str(instance.user.pk),
        context=context,
    )
