from django import forms
from django.core.validators import MinValueValidator
from django_api_forms import Form

from apps.core.models import UserAcquisition, Annotation


class CreateAnnotationForm(Form):
    user_acquisition_id = forms.ModelChoiceField(queryset=UserAcquisition.objects.all())
    title = forms.CharField()


class UpdateAnnotationForm(Form):
    title = forms.CharField()


class AnnotationItemForm(Form):
    annotation_id = forms.ModelChoiceField(queryset=Annotation.objects.all())
    content = forms.CharField()
    page = forms.IntegerField(validators=[MinValueValidator(1)])
