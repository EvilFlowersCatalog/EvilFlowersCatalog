from django import forms
from django_api_forms import Form

from apps.core.models import Entry


class ShelfRecordForm(Form):
    entry_id = forms.ModelChoiceField(queryset=Entry.objects.all())

    def clean_entry_id(self):
        self.fields['entry_id'].queryset = self.fields['entry_id'].queryset.filter(
            catalog__in=self.cleaned_data['user_id'].catalogs
        )
        return self.cleaned_data['entry_id']
