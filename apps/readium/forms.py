import logging

from django import forms
from django_api_forms import Form, BooleanField

from apps.core.models import Entry
from apps.readium.enums import LicenseAction
from apps.readium.models import License, Reservation

logger = logging.getLogger(__name__)


class CreateLicenseForm(Form):
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.filter(config__readium_enabled=True))
    duration = forms.DurationField()
    starts_at = forms.DateTimeField(required=False)


class UpdateLicenseForm(Form):
    """
    PUT /readium/v1/licenses/{id} body validator (IP-009 Phase 1).

    Canonical fields:
      action          one of LicenseAction
      requested_end   ISO-8601 datetime (renew action only)

    Legacy-shim fields (deprecated, accepted for one release per Q1
    resolution; the view emits a single deprecation log line when used):
      state           alias for `action` (string is identical — both
                      enums share values "active/returned/renewed/
                      revoked/cancelled"). Pre-IP-009 vocabulary.
      duration        ISO-8601 duration; translated to
                      `requested_end = now + duration` at the view layer.
    """

    action = forms.ChoiceField(choices=LicenseAction.choices, required=False)
    state = forms.ChoiceField(choices=LicenseAction.choices, required=False)
    requested_end = forms.DateTimeField(required=False)
    duration = forms.DurationField(required=False)

    def clean(self):
        cleaned = super().clean()

        # Resolve action: explicit `action` wins, then legacy `state`.
        action = cleaned.get("action")
        legacy_state = cleaned.get("state")
        if not action and legacy_state:
            cleaned["action"] = legacy_state
            logger.info(
                "license_update_legacy_state_field",
                extra={"resolved_action": legacy_state},
            )

        # Surface a deprecation marker so the view can log once per
        # request (we don't log here — clean() may be called multiple
        # times in some pytest paths).
        cleaned["_used_legacy_state"] = bool(legacy_state and not action)
        cleaned["_used_legacy_duration"] = bool(cleaned.get("duration") and not cleaned.get("requested_end"))
        return cleaned


class EncryptionTriggerForm(Form):
    force = BooleanField(required=False)


class CreateReservationForm(Form):
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.filter(config__readium_enabled=True))


class UpdateReservationForm(Form):
    """
    PATCH /reservations/{id} accepts only the user-driven status transitions:
        cancelled — user-cancel from queued or available
        claimed   — convert an available reservation into a license

    Server-side transitions (queued -> available, available -> expired) are not
    exposed through this form on purpose.
    """

    status = forms.ChoiceField(
        choices=[
            (Reservation.Status.CANCELLED, Reservation.Status.CANCELLED.label),
            (Reservation.Status.CLAIMED, Reservation.Status.CLAIMED.label),
        ]
    )
