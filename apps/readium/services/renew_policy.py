"""
Renewal policy (IP-003 Phase 3e).

`evaluate_renew` is consulted by the `LicenseRenewalsView` endpoint that the
Status Server's `renew_custom_url` template points at. STU policy:

- Deny if there is a non-terminal reservation (queue is non-empty) on the entry.
- Deny while the post-acquisition embargo runs (freshly-acquired titles cannot
  be renewed immediately) — see `renewable_from` for how long that is.
- Deny if the requested new end date exceeds
  EVILFLOWERS_READIUM_MAX_RENEW_DAYS from now.
- Otherwise allow and return the new end datetime.

Every denial carries a stable `code` (one of `RenewDenial`) so clients can
translate the reason instead of showing the English `reason` verbatim.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

from apps.readium.models import License, Reservation


class RenewDenial:
    TERMINAL_STATE = "terminal_state"
    RENEWAL_CAP = "renewal_cap"
    QUEUE = "queue"
    EMBARGO = "embargo"
    EXCEEDS_MAX_WINDOW = "exceeds_max_window"
    END_IN_PAST = "end_in_past"


@dataclass
class RenewDecision:
    allowed: bool
    new_end: Optional[datetime] = None
    reason: Optional[str] = None
    code: Optional[str] = None
    data: dict = field(default_factory=dict)

    @property
    def problem_data(self) -> dict:
        """`additional_data` for the 403 problem document: `{"code": ..., **data}`."""
        return {"code": self.code, **self.data}


def renewable_from(starts_at: datetime, expires_at: datetime, renewal_count: int) -> Optional[datetime]:
    """When the post-acquisition embargo lifts, or `None` if there is none.

    The embargo is `EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS`, capped at half the
    loan. Without the cap a 1-week loan (the portal offers 1 and 2 weeks) under
    a 7-day embargo could never be renewed: the embargo ended exactly when the
    loan did (#110). It is measured from `starts_at`, the same day-rounded clock
    as `expires_at`, not from `created_at`, which ran up to a day later.

    The embargo only guards the *first* renewal. After one, `expires_at` moves
    out, the loan gets longer and half of it grows — without the
    `renewal_count` guard the embargo would re-arm and block a second renewal
    that the first one had already cleared.
    """
    embargo_days = settings.EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS
    if not embargo_days or renewal_count > 0 or starts_at is None:
        return None

    embargo = timedelta(days=embargo_days)
    if expires_at is not None and expires_at > starts_at:
        embargo = min(embargo, (expires_at - starts_at) / 2)
    return starts_at + embargo


def evaluate_renew(license: License, requested_end: Optional[datetime] = None) -> RenewDecision:
    now = timezone.now()
    max_days = settings.EVILFLOWERS_READIUM_MAX_RENEW_DAYS
    # IP-009 Phase 5 E2: optional per-loan renewal cap (None = uncapped).
    max_renewals = getattr(settings, "EVILFLOWERS_READIUM_MAX_RENEWALS", None)

    if license.state in [License.LicenseState.REVOKED, License.LicenseState.CANCELLED, License.LicenseState.EXPIRED]:
        return RenewDecision(
            False,
            reason=f"License is in terminal state: {license.state}",
            code=RenewDenial.TERMINAL_STATE,
            data={"state": license.state},
        )

    if max_renewals is not None and license.renewal_count >= max_renewals:
        return RenewDecision(
            False,
            reason=f"Renewal cap reached ({license.renewal_count}/{max_renewals})",
            code=RenewDenial.RENEWAL_CAP,
            data={"renewal_count": license.renewal_count, "max_renewals": max_renewals},
        )

    has_queue = Reservation.objects.filter(
        entry=license.entry,
        status__in=[Reservation.Status.QUEUED, Reservation.Status.AVAILABLE],
    ).exists()
    if has_queue:
        return RenewDecision(False, reason="Other users are waiting for this title", code=RenewDenial.QUEUE)

    lifts_at = renewable_from(license.starts_at, license.expires_at, license.renewal_count)
    if lifts_at is not None and lifts_at > now:
        return RenewDecision(
            False,
            reason=f"This loan cannot be renewed before {lifts_at.isoformat()}",
            code=RenewDenial.EMBARGO,
            data={"renewable_from": lifts_at.isoformat()},
        )

    if requested_end is None:
        requested_end = now + timedelta(days=max_days)

    upper_bound = now + timedelta(days=max_days)
    if requested_end > upper_bound:
        return RenewDecision(
            False,
            reason=f"Requested end exceeds maximum renewal window of {max_days} days",
            code=RenewDenial.EXCEEDS_MAX_WINDOW,
            data={"max_renew_days": max_days},
        )

    if requested_end <= now:
        return RenewDecision(False, reason="Requested end must be in the future", code=RenewDenial.END_IN_PAST)

    return RenewDecision(True, new_end=requested_end)
