from django import forms
from django_api_forms import Form

from apps.core.fields.multirange import MultiRangeFormField
from apps.core.models import Acquisition, UserAcquisition


class UserAcquisitionForm(Form):
    acquisition_id = forms.ModelChoiceField(queryset=Acquisition.objects.all())
    type = forms.ChoiceField(choices=UserAcquisition.UserAcquisitionType.choices)
    range = MultiRangeFormField(required=False)
    expire_at = forms.DateTimeField(required=False)
