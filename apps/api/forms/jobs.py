from django import forms
from django_api_forms import AnyField, Form

DESIRED_STATES = [("completed", "completed"), ("cancelled", "cancelled")]


def _clean_spec(value):
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise forms.ValidationError("`spec` must be an object.")
    return value


class JobApplyForm(Form):
    """`POST /api/v1/jobs`: declare a job of `kind` on `subject_id` (created or updated)."""

    kind = forms.CharField(max_length=100)
    subject_id = forms.UUIDField(required=False)
    spec = AnyField(required=False)
    desired_state = forms.ChoiceField(choices=DESIRED_STATES, required=False)

    def clean_spec(self):
        return _clean_spec(self.cleaned_data.get("spec"))


class JobPatchForm(Form):
    """`PATCH /api/v1/jobs/{id}`: change the spec or the desired state."""

    spec = AnyField(required=False)
    desired_state = forms.ChoiceField(choices=DESIRED_STATES, required=False)

    def clean_spec(self):
        return _clean_spec(self.cleaned_data.get("spec"))
