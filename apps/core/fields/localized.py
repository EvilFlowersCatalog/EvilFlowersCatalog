import re
from typing import Dict, Optional

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import F, Func, TextField, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils.translation import gettext_lazy as _, get_language_from_request

# A text stored in several languages: {"sk": "Informatika", "en": "Computer science"}
LocalizedText = Dict[str, str]

LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2}$")


def normalize_localized(value) -> LocalizedText:
    """
    Coerce a value into a localized text mapping.

    A plain string (legacy API clients, imports) is stored under the default language,
    blank translations are dropped so an empty mapping means "no value".
    """
    if value is None:
        return {}

    if isinstance(value, str):
        value = value.strip()
        return {settings.EVILFLOWERS_DEFAULT_LANGUAGE: value} if value else {}

    if isinstance(value, dict):
        return {
            str(code).strip().lower(): str(text).strip()
            for code, text in value.items()
            if text is not None and str(text).strip()
        }

    raise TypeError(f"Cannot localize value of type {type(value).__name__}")


def localize(value, language: Optional[str] = None) -> Optional[str]:
    """
    Pick a single translation: the requested language, then the default language, then any.
    """
    value = normalize_localized(value)

    for code in (language, settings.EVILFLOWERS_DEFAULT_LANGUAGE):
        if code and value.get(code[:2].lower()):
            return value[code[:2].lower()]

    return next(iter(value.values()), None)


def request_language(request) -> str:
    """Language preferred by the client (Accept-Language), used for formats that carry a single title."""
    return get_language_from_request(request)


class LocalizedValues(Func):
    """
    All translations of a localized JSON column as one string, for (unaccented) text search.
    Unlike `column::text`, language codes are not part of the result.
    """

    arity = 1
    template = "jsonb_path_query_array(%(expressions)s, '$.*')::text"
    output_field = TextField()


class AnyLocalizedValue(Func):
    """First translation of a localized JSON column (in key order), as text."""

    arity = 1
    template = "jsonb_path_query_first(%(expressions)s, '$.*') #>> '{}'"
    output_field = TextField()


def localized_sort_key(field: str, language: Optional[str] = None, fallback: Optional[str] = None):
    """
    Case-insensitive sort key for a localized JSON column, following the same fallback as `localize`:
    the requested language, the default language, any translation, then the optional `fallback` column.
    """
    codes = []
    for code in (language, settings.EVILFLOWERS_DEFAULT_LANGUAGE):
        code = (code or "")[:2].lower()
        if LANGUAGE_CODE_PATTERN.match(code) and code not in codes:
            codes.append(code)

    candidates = [NullIf(KeyTextTransform(code, field), Value("")) for code in codes]
    candidates.append(AnyLocalizedValue(F(field)))
    if fallback:
        candidates.append(F(fallback))

    return Lower(Coalesce(*candidates, output_field=TextField()))


def sort_language(request) -> str:
    """Language to sort localized columns by: explicit `?language=` (the client's UI language) or Accept-Language."""
    return request.GET.get("language") or request_language(request)


class LocalizedTextFormField(forms.Field):
    """
    Accepts either a plain string or an object mapping ISO 639-1 codes to texts.
    Cleans to a localized text mapping.

    A non-empty value must contain the default language: other translations are optional,
    so every title can always be shown in the default language.
    """

    default_error_messages = {
        "invalid": _("Enter a text or an object mapping language codes to texts."),
        "invalid_language": _("'%(code)s' is not a valid ISO 639-1 language code."),
        "max_length": _("Translation '%(code)s' has at most %(max_length)d characters."),
        "default_language": _("Translation in the default language '%(code)s' is required."),
    }

    def __init__(self, *, max_length: Optional[int] = None, **kwargs):
        self.max_length = max_length
        super().__init__(**kwargs)

    def to_python(self, value) -> LocalizedText:
        if value in self.empty_values:
            return {}

        try:
            value = normalize_localized(value)
        except TypeError:
            raise ValidationError(self.error_messages["invalid"], code="invalid")

        for code, text in value.items():
            if not LANGUAGE_CODE_PATTERN.match(code):
                raise ValidationError(
                    self.error_messages["invalid_language"], code="invalid_language", params={"code": code}
                )
            if self.max_length is not None and len(text) > self.max_length:
                raise ValidationError(
                    self.error_messages["max_length"],
                    code="max_length",
                    params={"code": code, "max_length": self.max_length},
                )

        if value and settings.EVILFLOWERS_DEFAULT_LANGUAGE not in value:
            raise ValidationError(
                self.error_messages["default_language"],
                code="default_language",
                params={"code": settings.EVILFLOWERS_DEFAULT_LANGUAGE},
            )

        return value


__all__ = [
    "LocalizedText",
    "normalize_localized",
    "localize",
    "request_language",
    "LocalizedValues",
    "localized_sort_key",
    "sort_language",
    "LocalizedTextFormField",
]
