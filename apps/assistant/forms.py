from django import forms
from django_api_forms import Form

from apps.core.models import Catalog, Entry, UserAcquisition


class ChatForm(Form):
    #: Set when the conversation is opened from a publication page.
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all(), required=False)

    #: IP-015 D4: accepted because clients already send it, recorded on the
    #: chat, never branched on — the deployment serves a single catalog.
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all(), required=False)


class MessageForm(Form):
    message = forms.CharField(max_length=8000, strip=True)

    #: Publication the reader is asking about with this message, when they picked
    #: one ("ask about this book"). Can differ from message to message.
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all(), required=False)


class ExplainForm(Form):
    #: The reader's copy of the publication, as the viewer opened it.
    user_acquisition_id = forms.ModelChoiceField(
        queryset=UserAcquisition.objects.select_related("acquisition", "acquisition__entry")
    )
    text = forms.CharField(max_length=4000, strip=True)

    #: Viewer page the selection is on. Without it the model has to search for it.
    page = forms.IntegerField(min_value=1, required=False)

    #: Language code to explain in; the selection's own language when omitted.
    language = forms.CharField(max_length=10, required=False)
