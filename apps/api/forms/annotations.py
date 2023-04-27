from django import forms
from django_api_forms import Form

from apps.core.models import UserAcquisition


class CreateAnnotationForm(Form):
    user_acquisition_id = forms.ModelChoiceField(queryset=UserAcquisition.objects.all())
    content = forms.CharField()


class UpdateAnnotationForm(Form):
    content = forms.CharField()
