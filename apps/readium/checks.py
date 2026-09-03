"""
Deploy-time guards for Readium loan policy.

These settings interact, and some combinations are silently unusable rather
than loudly broken — the failure only shows up as a 403 on a renewal weeks
later. Fail at startup instead.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def check_renew_embargo_shorter_than_loan(app_configs, **kwargs):
    """`readium.E001` — an embargo that outlives the loan makes renewal unreachable.

    `evaluate_renew` denies while `created_at + embargo > now`, and separately
    denies once the license reaches the terminal `expired` state. If the embargo
    is not shorter than the loan, every loan expires before its embargo lifts,
    so the two rules overlap to reject *every* renewal — with a misleading
    "acquired within the last N days" reason.
    """
    embargo = getattr(settings, "EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS", 0)
    duration = getattr(settings, "EVILFLOWERS_READIUM_DEFAULT_BORROW_DURATION_DAYS", 0)

    if embargo and duration and embargo >= duration:
        return [
            Error(
                "EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS "
                f"({embargo}) must be less than "
                f"EVILFLOWERS_READIUM_DEFAULT_BORROW_DURATION_DAYS ({duration}).",
                hint=(
                    "With this combination no loan can ever be renewed: it reaches the "
                    "terminal `expired` state before the renewal embargo lifts. Lower the "
                    "embargo below the loan duration, or set it to 0 to disable it."
                ),
                id="readium.E001",
            )
        ]
    return []


@register()
def check_notification_base_url_is_public(app_configs, **kwargs):
    """`readium.W001` — e-mail links built on a loopback base URL are dead on arrival.

    Claim links and `.lcpl` download buttons are built from
    `EVILFLOWERS_BASE_URL` inside Celery workers (no request to derive the host
    from). The setting defaults to the runserver loopback; an August 2026 review
    round traced "the link in the e-mail does not work" to exactly that.
    """
    if not getattr(settings, "EVILFLOWERS_NOTIFICATIONS_ENABLED", False):
        return []
    base_url = str(getattr(settings, "EVILFLOWERS_BASE_URL", "") or "")
    loopback_markers = ("://127.0.0.1", "://localhost", "://0.0.0.0", "://[::1]")
    if not base_url or any(marker in base_url for marker in loopback_markers):
        return [
            Warning(
                f"EVILFLOWERS_BASE_URL ({base_url or 'unset'}) is not a public origin while notifications are enabled.",
                hint=(
                    "Links in notification e-mails (reservation claim, .lcpl download) are built from this "
                    "value and will not open for recipients. Set EVILFLOWERS_BASE_URL (or "
                    "EVILFLOWERS_READIUM_BASE_URL, which it falls back to) to the public https origin."
                ),
                id="readium.W001",
            )
        ]
    return []
