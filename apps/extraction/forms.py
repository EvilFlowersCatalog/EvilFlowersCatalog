import math

from django import forms
from django_api_forms import FieldList, Form, FormFieldList

from apps.extraction.models import EMBEDDING_DIMENSIONS, ParagraphLink, Region

_BOX_KEYS = ("x0", "y0", "x1", "y1")


def _box(value) -> dict:
    """`{x0, y0, x1, y1}` page fractions, origin top-left."""
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), (int, float)) and -0.01 <= value[key] <= 1.01 for key in _BOX_KEYS
    ):
        raise forms.ValidationError("Expected {x0, y0, x1, y1} as page fractions.")
    box = {key: min(max(float(value[key]), 0.0), 1.0) for key in _BOX_KEYS}
    if box["x1"] < box["x0"] or box["y1"] < box["y0"]:
        raise forms.ValidationError("A box must have x0 <= x1 and y0 <= y1.")
    return box


class StartRunForm(Form):
    page_count = forms.IntegerField(min_value=0)
    #: `[[width, height], ...]` in PDF points, one per page.
    page_sizes = forms.JSONField()
    #: The jobs the analyzer dispatched (`paragraphs`, `figures`, `equations`, `tables`).
    jobs = FieldList(forms.CharField(max_length=30))
    provenance = forms.JSONField(required=False)

    def clean_page_sizes(self):
        sizes = self.cleaned_data.get("page_sizes")
        if not isinstance(sizes, list) or not all(
            isinstance(size, list) and len(size) == 2 and all(isinstance(side, (int, float)) for side in size)
            for size in sizes
        ):
            raise forms.ValidationError("Expected [[width, height], ...].")
        return sizes

    def clean(self):
        # Absent fields are missing from `cleaned_data` altogether.
        self.cleaned_data["provenance"] = self.cleaned_data.get("provenance") or {}
        return self.cleaned_data


class RegionForm(Form):
    kind = forms.ChoiceField(choices=Region.Kind.choices)
    page = forms.IntegerField(min_value=1)
    seq = forms.IntegerField(min_value=0)
    bbox = forms.JSONField()
    image_path = forms.CharField(max_length=255, required=False)
    caption = forms.CharField(required=False)
    printed_caption = forms.CharField(required=False)
    description = forms.CharField(required=False)
    category = forms.CharField(max_length=50, required=False)
    markdown = forms.CharField(required=False)
    latex = forms.CharField(required=False)
    provenance = forms.JSONField(required=False)

    def clean_bbox(self):
        return _box(self.cleaned_data.get("bbox"))

    def clean(self):
        for key in ("image_path", "caption", "printed_caption", "description", "category", "markdown", "latex"):
            self.cleaned_data[key] = self.cleaned_data.get(key) or None
        provenance = self.cleaned_data["provenance"] = self.cleaned_data.get("provenance") or {}

        # Services that predate the two fields: image-service with captions on
        # sent the model's text as `caption` and the printed one in provenance;
        # every other service's `caption` is the printed one.
        if not self.cleaned_data["printed_caption"] and not self.cleaned_data["description"]:
            if provenance.get("caption_model"):
                self.cleaned_data["printed_caption"] = provenance.get("printed_caption") or None
                self.cleaned_data["description"] = self.cleaned_data["caption"]
            else:
                self.cleaned_data["printed_caption"] = self.cleaned_data["caption"]
        self.cleaned_data["caption"] = (
            self.cleaned_data["printed_caption"] or self.cleaned_data["description"] or self.cleaned_data["caption"]
        )
        return self.cleaned_data


class RegionsForm(Form):
    regions = FormFieldList(RegionForm, max_length=5000)


class SentenceForm(Form):
    seq = forms.IntegerField(min_value=0)
    #: Character offsets into the paragraph's text.
    start = forms.IntegerField(min_value=0)
    end = forms.IntegerField(min_value=0)
    page = forms.IntegerField(min_value=1)
    quads = forms.JSONField(required=False)

    def clean_quads(self):
        quads = self.cleaned_data.get("quads") or []
        if not isinstance(quads, list):
            raise forms.ValidationError("Expected a list of boxes.")
        return [_box(quad) for quad in quads]


class ParagraphForm(Form):
    page = forms.IntegerField(min_value=1)
    seq = forms.IntegerField(min_value=0)
    section = forms.CharField(required=False)
    bbox = forms.JSONField()
    text = forms.CharField(strip=False)
    # A list of numbers, checked in one pass in `clean_embedding`. `FieldList(FloatField())` ran a
    # Django field clean (with a deepcopy) per dimension, 1024 per paragraph and up to 1000 paragraphs
    # a request: form validation was ~5 % of the app's CPU on dev.
    embedding = forms.JSONField(required=False)
    sentences = FormFieldList(SentenceForm, required=False)

    def clean_bbox(self):
        return _box(self.cleaned_data.get("bbox"))

    def clean_embedding(self):
        embedding = self.cleaned_data.get("embedding") or None
        if embedding is None:
            return None
        if not isinstance(embedding, list) or not all(
            type(value) in (int, float) and math.isfinite(value) for value in embedding
        ):
            raise forms.ValidationError("Expected a list of finite numbers.")
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise forms.ValidationError(f"Expected {EMBEDDING_DIMENSIONS} dimensions, got {len(embedding)}.")
        return [float(value) for value in embedding]

    def clean(self):
        self.cleaned_data["section"] = self.cleaned_data.get("section") or None
        text = self.cleaned_data.get("text") or ""
        for sentence in self.cleaned_data.get("sentences") or []:
            if not sentence["start"] <= sentence["end"] <= len(text):
                raise forms.ValidationError("A sentence's offsets must fall inside its paragraph.")
        return self.cleaned_data


class ParagraphLinkForm(Form):
    #: `seq` of the paragraphs, as sent.
    source = forms.IntegerField(min_value=0)
    target = forms.IntegerField(min_value=0)
    kind = forms.ChoiceField(choices=ParagraphLink.Kind.choices)
    score = forms.FloatField(required=False)


class ParagraphsForm(Form):
    paragraphs = FormFieldList(ParagraphForm, max_length=1000)
    links = FormFieldList(ParagraphLinkForm, required=False)


class JobReportForm(Form):
    #: `done`, or `failed` when the job as a whole could not run.
    status = forms.ChoiceField(choices=[("done", "done"), ("failed", "failed")])
    error = forms.CharField(required=False)
    provenance = forms.JSONField(required=False)
