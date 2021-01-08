from django import forms
from django_api_forms import Form


class CatalogForm(Form):
    url_name = forms.SlugField()
    title = forms.CharField(max_length=100)
