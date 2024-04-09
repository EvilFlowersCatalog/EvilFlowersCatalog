from typing import Optional

import bibtexparser
from bibtexparser.bibdatabase import BibDatabase
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _
from django_api_forms import (
    Form,
    FormField,
    FileField,
    FormFieldList,
    ImageField,
    DictionaryField,
    BooleanField,
)

from apps.api.forms.category import CategoryForm
from apps.core.fields.partial_date import PartialDateFormField
from apps.core.models import Language, Author, Category, Currency, Feed, Catalog
from apps.core.models import Acquisition
from apps.core.validators import AvailableKeysValidator


class AuthorForm(Form):
    name = forms.CharField(max_length=255)
    surname = forms.CharField(max_length=255)


class CreateAuthorForm(AuthorForm):
    catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all())


class PriceForm(Form):
    currency_code = forms.ModelChoiceField(queryset=Currency.objects.all(), to_field_name="code")
    value = forms.DecimalField(max_digits=12, decimal_places=4)


class AcquisitionMetaForm(Form):
    relation = forms.ChoiceField(choices=Acquisition.AcquisitionType.choices, required=False)
    prices = FormFieldList(PriceForm, required=False)


class AcquisitionForm(AcquisitionMetaForm):
    content = FileField(mime=Acquisition.AcquisitionMIME.values)


class EntryConfigForm(Form):
    evilflowers_ocr_enabled = BooleanField(required=False)
    evilflowers_ocr_rewrite = BooleanField(required=False)
    evilflowers_annotations_create = BooleanField(required=False)
    evilflowers_viewer_print = BooleanField(required=False)
    evilflowers_share_enabled = BooleanField(required=False)
    evilflowres_metadata_fetch = BooleanField(required=False)
    evilflowers_render_type = forms.ChoiceField(
        required=False,
        choices=(
            ("document", _("Document render type")),
            ("page", _("Page render type")),
        ),
    )


class EntryForm(Form):
    class Meta:
        field_strategy = {"config": "django_api_forms.population_strategies.BaseStrategy"}

    id = forms.UUIDField(required=False)
    language_code = forms.CharField(max_length=3)
    category_ids = forms.ModelMultipleChoiceField(queryset=Category.objects.all(), required=False)
    authors = FormFieldList(AuthorForm, required=False)
    author_ids = forms.ModelMultipleChoiceField(queryset=Author.objects.all(), required=False)
    feeds = forms.ModelMultipleChoiceField(queryset=Feed.objects.all(), required=False)
    categories = FormFieldList(CategoryForm, required=False)
    title = forms.CharField(max_length=255)
    publisher = forms.CharField(max_length=255, required=False)
    published_at = PartialDateFormField(required=False)
    summary = forms.CharField(required=False)
    content = forms.CharField(required=False)
    acquisitions = FormFieldList(AcquisitionForm, required=False)
    identifiers = DictionaryField(
        required=False,
        validators=[AvailableKeysValidator(keys=settings.EVILFLOWERS_IDENTIFIERS)],
        value_field=forms.CharField(max_length=100, required=False, empty_value=None),
    )
    image = ImageField(
        max_length=settings.EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE,
        mime=settings.EVILFLOWERS_IMAGE_MIME,
        required=False,
    )
    config = FormField(EntryConfigForm, required=False)
    citation = forms.CharField(required=False)

    def clean_language_code(self) -> Language:
        language = Language.objects.filter(
            Q(alpha2=self.cleaned_data["language_code"]) | Q(alpha3=self.cleaned_data["language_code"])
        ).first()

        if not language:
            raise ValidationError("Language not found. Use valid alpha2 or alpha3 code.", "not-found")

        return language

    def clean_citation(self) -> Optional[str]:
        if not self.cleaned_data.get("citation"):
            return None

        bibtex = bibtexparser.loads(self.cleaned_data.get("citation"))

        if isinstance(bibtex, BibDatabase):
            try:
                bibtex.entries[0]
            except IndexError:
                raise ValidationError("No entry found in provided BibTeX", "no-bibtex-record")

            if len(bibtex.entries) > 1:
                raise ValidationError("BibTeX contains more than one record", "multiple-bibtex-records")

            return bibtexparser.dumps(bibtex)
        else:
            raise ValidationError("Invalid BibTeX record", "invalid-bibtex")

    def clean(self):
        if "author_ids" in self.cleaned_data.keys() and "authors" in self.cleaned_data.keys():
            raise ValidationError(
                _("You have to provide author_ids or authors objects (not both)"),
                "invalid",
            )

        if "category_ids" in self.cleaned_data.keys() and "categories" in self.cleaned_data.keys():
            raise ValidationError(
                _("You have to provide category_ids or categories object (not both)"),
                "invalid",
            )

        return self.cleaned_data
