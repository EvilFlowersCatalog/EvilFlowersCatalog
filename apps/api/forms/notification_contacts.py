from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.translation import gettext_lazy as _
from django_api_forms import Form

from apps.notifications.models import NotificationContact


class NotificationContactForm(Form):
    type = forms.ChoiceField(choices=NotificationContact.ContactType.choices)
    value = forms.CharField(max_length=255)
    is_primary = forms.BooleanField(required=False, initial=True)

    def clean(self):
        cleaned_data = super().clean()

        if cleaned_data.get("type") == NotificationContact.ContactType.EMAIL and cleaned_data.get("value"):
            try:
                validate_email(cleaned_data["value"])
            except ValidationError:
                self.add_error(("value",), ValidationError(_("Enter a valid email address."), code="invalid"))

        return cleaned_data
