from django.conf import settings
from django.db import transaction
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

    # IP-008 Phase 3 C2: defer the dispatch to after the surrounding
    # transaction commits. If LCP issuance fails and the transaction is
    # rolled back, the License row never exists and the email is never
    # sent. Outside a transaction `on_commit` fires immediately so the
    # behaviour is identical for callers that don't use atomic().
    recipient_user_id = str(instance.user.pk)
    # Attach the .lcpl so the user can open the loan straight from the email on a
    # tablet/reader — the capability-token gateway URL can't be embedded in mail.
    # LCP-compliant: the file is inert without the passphrase (only the hint ships).
    from apps.readium.notifications import lcpl_attachment_ref

    attachment_refs = [lcpl_attachment_ref(instance.pk)]
    transaction.on_commit(
        lambda: send_notification.delay(
            notification_type="license_created",
            recipient_user_id=recipient_user_id,
            context=context,
            attachment_refs=attachment_refs,
        )
    )
