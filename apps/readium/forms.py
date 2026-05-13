from django import forms
from django_api_forms import Form, BooleanField

from apps.core.models import Entry
from apps.readium.models import License, Reservation


class CreateLicenseForm(Form):
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.filter(config__readium_enabled=True))
    duration = forms.DurationField()
    starts_at = forms.DateTimeField(required=False)


class UpdateLicenseForm(Form):
    state = forms.ChoiceField(choices=License.LicenseState.choices, required=False)
    duration = forms.DurationField(required=False)


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
