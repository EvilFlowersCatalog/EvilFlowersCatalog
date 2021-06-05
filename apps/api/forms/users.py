from django import forms
from django_api_forms import Form, BooleanField


class UserForm(Form):
    name = forms.CharField(max_length=30)
    surname = forms.CharField(max_length=150)
    password = forms.CharField(required=False)
    is_active = BooleanField(required=False)


class CreateUserForm(UserForm):
    email = forms.EmailField()
    password = forms.CharField(required=True)
