from django import forms
from django_api_forms import AnyField, BooleanField, Form


def _clean_options(value):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise forms.ValidationError("`options` must be an object.")
    return value


class JobStartForm(Form):
    kind = forms.CharField(max_length=100)
    subject_id = forms.UUIDField(required=False)
    options = AnyField(required=False)
    force = BooleanField(required=False)

    def clean_options(self):
        return _clean_options(self.cleaned_data.get("options"))


class JobRetryForm(Form):
    options = AnyField(required=False)

    def clean_options(self):
        return _clean_options(self.cleaned_data.get("options"))


class JobRevokeForm(Form):
    terminate = BooleanField(required=False)


class JobActionForm(Form):
    options = AnyField(required=False)

    def clean_options(self):
        return _clean_options(self.cleaned_data.get("options"))
