from django import forms
from django_api_forms import Form, BooleanField, EnumField, FormFieldList

from apps.core.models import User, UserCatalog


class UserCatalogForm(Form):
    user_id = forms.ModelChoiceField(queryset=User.objects.all())
    mode = EnumField(UserCatalog.Mode)


class CatalogForm(Form):
    url_name = forms.SlugField()
    title = forms.CharField(max_length=100)
    is_public = BooleanField(required=False)
    users = FormFieldList(UserCatalogForm, required=False)

    def clean_url_name(self) -> str:
        return self.cleaned_data["url_name"].lower()
