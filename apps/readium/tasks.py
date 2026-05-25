"""
Readium Celery tasks (IP-003, IP-009).

- `sweep_unclaimed_reservations` (every minute): expire any reservation whose
  claim deadline has passed and promote the next user in line.
- `notify_expiring_licenses` (daily 06:00 UTC): send `license_expiring_soon`
  for any active license expiring within EVILFLOWERS_READIUM_EXPIRY_REMINDER_DAYS,
  deduplicated via NotificationLog so the user is reminded at most once per
  (license, reminder window).
- `expire_lapsed_licenses` (every 5 minutes, IP-009 Phase 2): transition
  READY/ACTIVE licenses whose `expires_at` is in the past to `EXPIRED`,
  releasing the partial UniqueConstraint so the user can re-borrow.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
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


@shared_task
def expire_lapsed_licenses() -> int:
    """
    IP-009 Phase 2: transition naturally-expired licenses out of the
    non-terminal states so the partial `UniqueConstraint(entry, user,
    state__in=("ready","active"))` releases and the user can borrow
    again.

    Runs every 5 minutes (`evil_flowers_catalog/celery.py`). Sibling
    on-read fallback in `LicenseService.can_user_borrow` covers the
    gap between sweeps.
    """
    from apps.readium.models import License
    from apps.readium.services import LicenseService

    now = timezone.now()
    transitioned = 0
    errors = 0

    candidate_ids = list(
        License.objects.filter(
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            expires_at__lt=now,
        ).values_list("pk", flat=True)
    )

    for pk in candidate_ids:
        try:
            with transaction.atomic():
                license_obj = License.objects.select_for_update().get(pk=pk)
                # Re-check inside the lock — another sweep / borrow may
                # have already transitioned it.
                if license_obj.state not in (License.LicenseState.READY, License.LicenseState.ACTIVE):
                    continue
                if license_obj.expires_at >= now:
                    continue
                license_obj.state = License.LicenseState.EXPIRED
                license_obj.save(update_fields=["state", "updated_at"])
                transitioned += 1
            # Best-effort queue promotion outside the transaction.
            LicenseService._maybe_promote_next(license_obj)
        except Exception:
            errors += 1
            logger.exception("expire_lapsed_licenses failed for license %s", pk)

    logger.info(
        "readium.expire_sweep",
        extra={
            "event": "readium.expire_sweep",
            "transitioned": transitioned,
            "errors": errors,
            "candidates": len(candidate_ids),
        },
    )
    return transitioned
