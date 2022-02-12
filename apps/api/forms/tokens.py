from django import forms
from django_api_forms import Form


class AccessTokenForm(Form):
    username = forms.EmailField()
    password = forms.CharField()


class RefreshTokenForm(Form):
    refresh = forms.CharField()
