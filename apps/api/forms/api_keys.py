from django import forms
from django_api_forms import Form, BooleanField

from apps.core.models import ApiKey, User


class ApiKeyForm(Form):
    user_id = forms.ModelChoiceField(queryset=User.objects.all(), required=False)
    name = forms.CharField(max_length=100, required=False)
    is_active = BooleanField(required=False)
    #: Where the key's client sends the text it reads (#174). Empty inherits `content.external_processor_default`.
    processor = forms.ChoiceField(choices=ApiKey.Processor.choices, required=False)
