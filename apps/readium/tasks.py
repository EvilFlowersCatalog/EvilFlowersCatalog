"""
Readium Celery tasks (IP-003).

- `sweep_unclaimed_reservations` (every minute): expire any reservation whose
  claim deadline has passed and promote the next user in line.
- `notify_expiring_licenses` (daily 06:00 UTC): send `license_expiring_soon`
  for any active license expiring within EVILFLOWERS_READIUM_EXPIRY_REMINDER_DAYS,
  deduplicated via NotificationLog so the user is reminded at most once per
  (license, reminder window).
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def sweep_unclaimed_reservations() -> int:
    """Expire `available` reservations past their claim_deadline; promote next."""
    from apps.readium.services import ReservationService

    expired = ReservationService.expire_unclaimed()
    if expired:
        logger.info("Expired %d unclaimed reservations", expired)
    return expired


@shared_task
def notify_expiring_licenses() -> int:
    """
    Send `license_expiring_soon` to every active license expiring within
    EVILFLOWERS_READIUM_EXPIRY_REMINDER_DAYS. Idempotent — NotificationLog
    suppresses duplicates for the same (license, day).
    """
    from apps.notifications.models import NotificationLog
    from apps.notifications.services import NotificationService
    from apps.readium.models import License

    reminder_days = settings.EVILFLOWERS_READIUM_EXPIRY_REMINDER_DAYS
    now = timezone.now()
    threshold = now + timedelta(days=reminder_days)

    candidates = License.objects.select_related("entry", "user").filter(
        state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
        expires_at__gt=now,
        expires_at__lte=threshold,
    )

    sent = 0
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for license_obj in candidates:
        # Skip if we already logged an expiring-soon notification for this license today.
        already_sent = NotificationLog.objects.filter(
            recipient=license_obj.user,
            notification_type="license_expiring_soon",
            created_at__gte=today_start,
            context_snapshot__license_id=str(license_obj.pk),
        ).exists()
        if already_sent:
            continue

        context = {
            "user_name": license_obj.user.full_name or license_obj.user.username,
            "entry_title": license_obj.entry.title,
            "entry_author": license_obj.entry.first_author_name,
            "expires_at": license_obj.expires_at.isoformat() if license_obj.expires_at else "",
            "days_left": (license_obj.expires_at - now).days if license_obj.expires_at else None,
            "license_id": str(license_obj.pk),
        }
        NotificationService.send("license_expiring_soon", license_obj.user, context)
        sent += 1

    if sent:
        logger.info("Sent %d license_expiring_soon notifications", sent)
    return sent
