from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django_api_forms import Form, FormField, FileField, FormFieldList

from apps.core.models import Language, Author, Category, Currency, Feed
from apps.core.models import Acquisition


class CategoryForm(Form):
    term = forms.CharField(max_length=255)
    label = forms.CharField(max_length=255, required=False)
    scheme = forms.CharField(max_length=255, required=False)


class AuthorForm(Form):
    name = forms.CharField(max_length=255)
    surname = forms.CharField(max_length=255)


class PriceForm(Form):
    currency_code = forms.ModelChoiceField(queryset=Currency.objects.all(), to_field_name='code')
    value = forms.DecimalField(max_digits=12, decimal_places=4)


class AcquisitionMetaForm(Form):
    relation = forms.ChoiceField(choices=Acquisition.AcquisitionType.choices, required=False)
    prices = FormFieldList(PriceForm, required=False)


class AcquisitionForm(AcquisitionMetaForm):
    content = FileField(mime=Acquisition.AcquisitionMIME.values)


class EntryForm(Form):
    language_code = forms.ModelChoiceField(queryset=Language.objects.all(), to_field_name='code')
    author_id = forms.ModelChoiceField(queryset=Author.objects.all(), required=False)
    author = FormField(AuthorForm, required=False)
    category_id = forms.ModelChoiceField(queryset=Category.objects.all(), required=False)
    contributors = FormFieldList(AuthorForm, required=False)
    contributor_ids = forms.ModelMultipleChoiceField(queryset=Author.objects.all(), required=False)
    feeds = forms.ModelMultipleChoiceField(queryset=Feed.objects.all(), required=False)
    category = FormField(CategoryForm, required=False)
    title = forms.CharField(max_length=255)
    summary = forms.CharField(required=False)
    content = forms.CharField(required=False)
    acquisitions = FormFieldList(AcquisitionForm, required=False)

    def clean(self):
        if 'author_id' in self.cleaned_data.keys() and 'author' in self.cleaned_data.keys():
            raise ValidationError(_("You have to provide author_id or author object (not both)"), 'invalid')

        if 'category_id' in self.cleaned_data.keys() and 'category' in self.cleaned_data.keys():
            raise ValidationError(_("You have to provide category_id or category object (not both)"), 'invalid')

        if 'contributor_ids' in self.cleaned_data.keys() and 'contributors' in self.cleaned_data.keys():
            raise ValidationError(
                _("You have to provide contributor_ids or contributors object (not both)"), 'invalid'
            )

        return self.cleaned_data
