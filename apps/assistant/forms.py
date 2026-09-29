from django import forms
from django_api_forms import Form

from apps.core.models import Catalog, Entry


class ChatForm(Form):
    #: Set when the conversation is opened from a publication page.
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all(), required=False)

    #: IP-015 D4: accepted because clients already send it, recorded on the
    #: chat, never branched on — the deployment serves a single catalog.
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all(), required=False)


class MessageForm(Form):
    message = forms.CharField(max_length=8000, strip=True)
