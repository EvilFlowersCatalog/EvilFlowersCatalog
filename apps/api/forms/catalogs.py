from django import forms
from django_api_forms import Form, BooleanField


class CatalogForm(Form):
    url_name = forms.SlugField()
    title = forms.CharField(max_length=100)
    is_public = BooleanField(required=False)
