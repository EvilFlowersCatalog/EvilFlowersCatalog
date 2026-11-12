from django import forms
from django.conf import settings
from django_api_forms import Form

from apps.core.models import EntryReview, Language


class EntryReviewForm(Form):
    rating = forms.IntegerField(min_value=EntryReview.RATING_MIN, max_value=EntryReview.RATING_MAX)
    title = forms.CharField(max_length=255, required=False)
    body = forms.CharField(required=False)
    language_code = forms.ModelChoiceField(queryset=Language.objects.all(), to_field_name="alpha2", required=False)

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        limit = settings.EVILFLOWERS_REVIEWS_BODY_MAX_CHARS
        if len(body) > limit:
            raise forms.ValidationError(f"At most {limit} characters.", code="max_length")
        return body

    def clean_title(self):
        return (self.cleaned_data.get("title") or "").strip()


class EntryReviewModerationForm(Form):
    state = forms.ChoiceField(choices=EntryReview.State.choices)
    moderation_note = forms.CharField(required=False)
