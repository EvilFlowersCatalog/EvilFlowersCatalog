from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django_api_forms import Form

from apps.core.models import Catalog, Feed, Entry


class FeedForm(Form):
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all())
    title = forms.CharField(max_length=100)
    url_name = forms.SlugField(max_length=50)
    kind = forms.ChoiceField(choices=Feed.FeedKind.choices)
    content = forms.CharField()
    per_page = forms.IntegerField(min_value=1, required=False)
    entries = forms.ModelMultipleChoiceField(
        queryset=Entry.objects.all(), required=False
    )
    parents = forms.ModelMultipleChoiceField(
        queryset=Feed.objects.filter(kind=Feed.FeedKind.NAVIGATION), required=False
    )

    def clean(self):
        if (
            self.cleaned_data.get("entries")
            and self.cleaned_data["kind"] == Feed.FeedKind.NAVIGATION
        ):
            raise ValidationError(_("Navigation feed cannot have entries"))
        return self.cleaned_data
