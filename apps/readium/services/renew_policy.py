"""
Renewal policy (IP-003 Phase 3e).

`evaluate_renew` is consulted by the `LicenseRenewalsView` endpoint that the
Status Server's `renew_custom_url` template points at. STU policy:

- Deny if there is a non-terminal reservation (queue is non-empty) on the entry.
- Deny if the license was created within EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS
  (freshly-acquired titles cannot be renewed immediately).
- Deny if the requested new end date exceeds
  EVILFLOWERS_READIUM_MAX_RENEW_DAYS from now.
- Otherwise allow and return the new end datetime.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

from apps.readium.models import License, Reservation


@dataclass
class RenewDecision:
    allowed: bool
    new_end: Optional[datetime] = None
    reason: Optional[str] = None


def evaluate_renew(license: License, requested_end: Optional[datetime] = None) -> RenewDecision:
    now = timezone.now()
    max_days = settings.EVILFLOWERS_READIUM_MAX_RENEW_DAYS
    embargo_days = settings.EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS

    if license.state in [License.LicenseState.REVOKED, License.LicenseState.CANCELLED, License.LicenseState.EXPIRED]:
        return RenewDecision(False, reason=f"License is in terminal state: {license.state}")

    has_queue = Reservation.objects.filter(
        entry=license.entry,
        status__in=[Reservation.Status.QUEUED, Reservation.Status.AVAILABLE],
    ).exists()
    if has_queue:
        return RenewDecision(False, reason="Other users are waiting for this title")

    if license.created_at is not None and license.created_at + timedelta(days=embargo_days) > now:
        return RenewDecision(
            False,
            reason=f"License acquired within the last {embargo_days} days cannot be renewed yet",
        )

    if requested_end is None:
        requested_end = now + timedelta(days=max_days)

    upper_bound = now + timedelta(days=max_days)
    if requested_end > upper_bound:
        return RenewDecision(
            False,
            reason=f"Requested end exceeds maximum renewal window of {max_days} days",
        )

    if requested_end <= now:
        return RenewDecision(False, reason="Requested end must be in the future")

    return RenewDecision(True, new_end=requested_end)
