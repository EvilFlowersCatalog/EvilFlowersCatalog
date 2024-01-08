from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _


@deconstructible
class AvailableKeysValidator:
    """A customized validator designed for HStore to restrict keys."""

    messages = {
        "unsupported_keys": _("Some keys are not supported: %(keys)s"),
    }
    strict = False

    def __init__(self, keys, messages=None):
        self.keys = set(keys)
        if messages is not None:
            self.messages = {**self.messages, **messages}

    def __call__(self, value):
        keys = set(value)
        unsupported_keys = keys - self.keys
        if unsupported_keys:
            raise ValidationError(
                self.messages["unsupported_keys"],
                code="unsupported_keys",
                params={"keys": ", ".join(unsupported_keys)},
            )

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.keys == other.keys and self.messages == other.messages
