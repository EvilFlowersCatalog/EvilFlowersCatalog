from django import forms
from django_api_forms import Form

from apps.core.models import Catalog


class CategoryForm(Form):
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all(), required=False)
    term = forms.CharField(max_length=255)
    label = forms.CharField(max_length=255, required=False)
    scheme = forms.CharField(max_length=255, required=False)
