from django import forms
from django_api_forms import Form, BooleanField


class UserForm(Form):
    class Meta:
        field_strategy = {
            "password": "django_api_forms.population_strategies.IgnoreStrategy",
            "lcp_passphrase": "django_api_forms.population_strategies.IgnoreStrategy",
        }

    name = forms.CharField(max_length=30)
    surname = forms.CharField(max_length=150)
    password = forms.CharField(required=False)
    is_active = BooleanField(required=False)
    lcp_passphrase = forms.CharField(required=False, min_length=4, max_length=255)
    lcp_passphrase_hint = forms.CharField(required=False, max_length=255)


class CreateUserForm(UserForm):
    username = forms.CharField(max_length=200)
    password = forms.CharField(required=True)
