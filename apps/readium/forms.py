from django import forms
from django_api_forms import Form

from apps.core.models import Entry
from apps.readium.models import License


class CreateLicenseForm(Form):
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.filter(config__readium_enabled=True))
    state = forms.ChoiceField(choices=License.LicenseState.choices)
    duration = forms.DurationField()
    starts_at = forms.DateTimeField(required=False)


class UpdateLicenseForm(Form):
    state = forms.ChoiceField(choices=License.LicenseState.choices)
    duration = forms.DurationField()
