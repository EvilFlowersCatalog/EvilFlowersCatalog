from django import forms
from django_api_forms import Form

from apps.core.models import Entry


class ShelfRecordForm(Form):
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all())
