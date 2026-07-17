"""
Deploy-time guards for Readium loan policy.

These settings interact, and some combinations are silently unusable rather
than loudly broken — the failure only shows up as a 403 on a renewal weeks
later. Fail at startup instead.
"""

from django.conf import settings
from django.core.checks import Error, register


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
