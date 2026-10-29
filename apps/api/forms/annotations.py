from django import forms
from django.core.validators import MinValueValidator
from django_api_forms import Form

from apps.core.models import UserAcquisition, Annotation


class CreateAnnotationForm(Form):
    user_acquisition_id = forms.ModelChoiceField(queryset=UserAcquisition.objects.all())
    title = forms.CharField()
    #: Agent annotations are created by the assistant only (`/assistant/v1/highlights`).
    kind = forms.ChoiceField(
        choices=[(kind, label) for kind, label in Annotation.Kind.choices if kind != Annotation.Kind.AGENT],
        required=False,
    )

    def clean_kind(self):
        return self.cleaned_data.get("kind") or Annotation.Kind.DRAWING


class UpdateAnnotationForm(Form):
    title = forms.CharField()


class AnnotationItemForm(Form):
    annotation_id = forms.ModelChoiceField(queryset=Annotation.objects.all())
    content = forms.CharField()
    page = forms.IntegerField(validators=[MinValueValidator(1)])

    # A reader's own text highlight (kind `highlight`, IP-018): the selected
    # text and one box per line, so it can be listed and jumped to.
    text = forms.CharField(required=False)
    quads = forms.JSONField(required=False)

    def clean_quads(self):
        quads = self.cleaned_data.get("quads")
        if quads in (None, ""):
            return None
        keys = ("x0", "y0", "x1", "y1")
        if not isinstance(quads, list) or not all(
            isinstance(quad, dict)
            and all(isinstance(quad.get(key), (int, float)) and 0 <= quad[key] <= 1 for key in keys)
            for quad in quads
        ):
            raise forms.ValidationError("`quads` must be a list of {x0, y0, x1, y1} page fractions.")
        return [{key: float(quad[key]) for key in keys} for quad in quads]

    def clean_text(self):
        return self.cleaned_data.get("text") or None
