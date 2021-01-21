from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django_api_forms import Form

from apps.core.models import Catalog, Feed


class FeedForm(Form):
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all())
    title = forms.CharField(max_length=100)
    url_name = forms.SlugField(max_length=50)
    content = forms.CharField()

    def clean_url_name(self):
        if self.cleaned_data['url_name'] == 'new':
            raise ValidationError(
                _("URL name (url_name property) is reserved for http://opds-spec.org/sort/new implementation"),
                code="reserved"
            )

        if self.cleaned_data['url_name'] == 'popular':
            raise ValidationError(
                _("URL name (url_name property) is reserved for http://opds-spec.org/sort/popular implementation"),
                code="reserved"
            )

        return self.cleaned_data['url_name']
