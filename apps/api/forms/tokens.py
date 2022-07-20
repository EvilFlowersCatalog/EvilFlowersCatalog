from django import forms
from django_api_forms import Form


class AccessTokenForm(Form):
    username = forms.CharField(max_length=200)
    password = forms.CharField()


class RefreshTokenForm(Form):
    refresh = forms.CharField()
