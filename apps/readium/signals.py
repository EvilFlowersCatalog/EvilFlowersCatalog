"""
Lifecycle notification signals (IP-003 Phase 4b).

Wires Django post_save signals on License, Reservation, and User to the
IP-002 NotificationService. All sends go through Celery via
`apps.notifications.tasks.send_notification`.

Transition detection uses `update_fields` when available; otherwise we keep
the previous status on the instance via `__original_status` set on pre_save.
"""

import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.core.models import Entry, User
from apps.readium.models import License, Reservation

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return getattr(settings, "EVILFLOWERS_NOTIFICATIONS_ENABLED", False)


def _enqueue(notification_type: str, user, context: dict) -> None:
    from apps.notifications.tasks import send_notification

    try:
        send_notification.delay(
            notification_type=notification_type,
            recipient_user_id=str(user.pk),
            context=context,
        )
    except Exception:  # pragma: no cover — best effort
        logger.exception("Failed to enqueue notification %s for user %s", notification_type, user.pk)


# License state transitions ---------------------------------------------------


@receiver(pre_save, sender=License)
def _stash_original_license_state(sender, instance: License, **kwargs):
    if instance.pk is None:
        instance._original_state = None
        return
    try:
        instance._original_state = License.objects.only("state").get(pk=instance.pk).state
    except License.DoesNotExist:
        instance._original_state = None


@receiver(post_save, sender=License)
def _notify_license_transitions(sender, instance: License, created: bool, **kwargs):
    if not _enabled() or created:
        return

    previous = getattr(instance, "_original_state", None)
    if previous == instance.state:
        return

    base_context = {
        "user_name": instance.user.full_name or instance.user.username,
        "entry_title": instance.entry.title,
        "entry_author": instance.entry.first_author_name,
        "license_id": str(instance.pk),
    }

    if instance.state == License.LicenseState.RETURNED:
        _enqueue("license_returned", instance.user, base_context)
    elif instance.state == License.LicenseState.REVOKED:
        _enqueue("license_revoked", instance.user, base_context | {"reason": ""})
    elif previous in [License.LicenseState.READY, License.LicenseState.ACTIVE]:
        # Renewal is detected as a state-stable update with new expires_at — handled
        # by the LicenseRenewalsView path. We do not fire here to avoid spam.
        return


# Reservation state transitions -----------------------------------------------


@receiver(pre_save, sender=Reservation)
def _stash_original_reservation_status(sender, instance: Reservation, **kwargs):
    if instance.pk is None:
        instance._original_status = None
        return
    try:
        instance._original_status = Reservation.objects.only("status").get(pk=instance.pk).status
    except Reservation.DoesNotExist:
        instance._original_status = None


@receiver(post_save, sender=Reservation)
def _notify_reservation_transitions(sender, instance: Reservation, created: bool, **kwargs):
    if not _enabled():
        return

    base_context = {
        "user_name": instance.user.full_name or instance.user.username,
        "entry_title": instance.entry.title,
        "entry_author": instance.entry.first_author_name,
        "reservation_id": str(instance.pk),
    }

    if created and instance.status == Reservation.Status.QUEUED:
        from apps.readium.services.entry_lcp_decorator import (
            reservation_eta_earliest,
            reservation_eta_latest,
        )

        eta_from = reservation_eta_earliest(instance.entry, instance.position)
        eta_until = reservation_eta_latest(instance.entry, instance.position)
        context = base_context | {
            "position": instance.position,
            "claim_window_hours": settings.EVILFLOWERS_READIUM_RESERVATION_CLAIM_HOURS,
            "estimated_available_from": eta_from.isoformat() if eta_from else "",
            "estimated_available_until": eta_until.isoformat() if eta_until else "",
        }
        _enqueue("reservation_placed", instance.user, context)
        return

    previous = getattr(instance, "_original_status", None)
    if previous == instance.status:
        return

    if instance.status == Reservation.Status.AVAILABLE:
        from apps.notifications.services import NotificationService
        from apps.readium.views.claim import CLAIM_SCOPE

        claim_url = NotificationService.generate_scoped_url(
            user_id=str(instance.user.pk),
            scope=CLAIM_SCOPE,
            resource_path=f"/readium/v1/reservations/{instance.pk}/claim",
        )
        context = base_context | {
            "claim_deadline": instance.claim_deadline.isoformat() if instance.claim_deadline else "",
            "claim_url": claim_url,
        }
        _enqueue("reservation_available", instance.user, context)
    elif instance.status == Reservation.Status.EXPIRED:
        _enqueue("reservation_expired", instance.user, base_context)
    elif instance.status == Reservation.Status.CANCELLED:
        _enqueue("reservation_cancelled", instance.user, base_context)


# Passphrase change ----------------------------------------------------------


@receiver(pre_save, sender=User)
def _stash_original_passphrase_hash(sender, instance: User, **kwargs):
    if instance.pk is None:
        instance._original_passphrase_hash = None
        return
    try:
        instance._original_passphrase_hash = (
            User.objects.only("lcp_passphrase_hash").get(pk=instance.pk).lcp_passphrase_hash
        )
    except User.DoesNotExist:
        instance._original_passphrase_hash = None


@receiver(post_save, sender=User)
def _notify_passphrase_change(sender, instance: User, created: bool, **kwargs):
    if not _enabled() or created:
        return
    previous = getattr(instance, "_original_passphrase_hash", None)
    new_value = instance.lcp_passphrase_hash
    if previous == new_value:
        return
    if not new_value:
        # Only notify on set/rotate, not on clear.
        return

    context = {
        "user_name": instance.full_name or instance.username,
    }
    _enqueue("passphrase_changed", instance, context)


# Entry encryption trigger (IP-008 Phase 3 D2) -------------------------------
#
# Moved from `apps/core/models/entry.py` to reverse the cross-app import
# direction. `apps.core` no longer imports `apps.readium.services`; the
# readium app subscribes to the core-owned `Entry.post_save` signal here.


@receiver(post_save, sender=Entry)
def _trigger_readium_encryption(sender, instance: Entry, **kwargs):
    """Trigger LCP encryption when readium_enabled is set on an entry with existing acquisitions."""
    if not instance.read_config("readium_enabled"):
        return

    from apps.readium.services import ContentEncryptionService

    for acquisition in instance.acquisitions.filter(mime__in=["application/epub+zip", "application/pdf"]):
        if not acquisition.content or hasattr(acquisition, "encrypted_content"):
            continue

        try:
            ContentEncryptionService.encrypt_acquisition(acquisition)
            logger.info("Triggered LCP encryption for acquisition %s", acquisition.pk)
        except ValueError as e:
            logger.warning("Failed to trigger encryption for acquisition %s: %s", acquisition.pk, e)
