from django import forms
from django_api_forms import Form

from apps.assistant import contexts
from apps.core.models import Catalog, Entry, UserAcquisition


def _user_acquisitions():
    return UserAcquisition.objects.select_related("acquisition", "acquisition__entry")


class ChatForm(Form):
    #: Set when the conversation is opened from a publication page.
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all(), required=False)

    #: IP-015 D4: accepted because clients already send it, recorded on the
    #: chat, never branched on — the deployment serves a single catalog.
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all(), required=False)

    #: The reader's copy, when the chat is opened in the viewer. Lets the
    #: assistant read the document; `entry_id` defaults to its publication.
    user_acquisition_id = forms.ModelChoiceField(queryset=_user_acquisitions(), required=False)


class MessageForm(Form):
    message = forms.CharField(max_length=8000, strip=True)

    #: Publication the reader is asking about with this message, when they picked
    #: one ("ask about this book"). Can differ from message to message.
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all(), required=False)

    #: What the reader attached in the viewer (`apps.assistant.contexts`).
    contexts = forms.JSONField(required=False)

    #: Viewer page the reader is on.
    page = forms.IntegerField(min_value=1, required=False)

    #: The reader's current copy of the chat's publication. The portal issues
    #: a new one every time the viewer opens, so a chat continued in the
    #: viewer later re-binds to it.
    user_acquisition_id = forms.ModelChoiceField(queryset=_user_acquisitions(), required=False)

    def clean_contexts(self):
        try:
            return contexts.normalize(self.cleaned_data.get("contexts"))
        except contexts.InvalidContext as error:
            raise forms.ValidationError(str(error))


class ExplainForm(Form):
    #: The reader's copy of the publication, as the viewer opened it.
    user_acquisition_id = forms.ModelChoiceField(queryset=_user_acquisitions())
    text = forms.CharField(max_length=4000, strip=True)

    #: Viewer page the selection is on. Without it the model has to search for it.
    page = forms.IntegerField(min_value=1, required=False)

    #: Language code to explain in; the selection's own language when omitted.
    language = forms.CharField(max_length=10, required=False)
