"""Argument coercion for tool calls.

The `inputSchema` we advertise is a contract, not an enforcement mechanism —
nothing in the stack validates a client's `arguments` against it, and models
routinely send a string where an integer belongs or invent a filter that does
not exist. `Arguments` re-checks every value the handlers actually read and
raises `ToolError`, so a sloppy call comes back as a readable message the model
can act on instead of a 500.
"""

import uuid as uuid_module
from typing import Any, Iterable, List, Optional, Sequence

from django.utils.translation import gettext as _

from apps.mcp.errors import ToolError


class Arguments:
    def __init__(self, raw: Any, *, allowed: Iterable[str]):
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ToolError(_("`arguments` must be an object."))

        allowed = set(allowed)
        unknown = sorted(set(raw.keys()) - allowed)
        if unknown:
            raise ToolError(
                _("Unknown argument(s): %(unknown)s. Accepted arguments: %(allowed)s.")
                % {"unknown": ", ".join(unknown), "allowed": ", ".join(sorted(allowed))}
            )

        self._raw = raw

    def _present(self, name: str) -> bool:
        return name in self._raw and self._raw[name] is not None and self._raw[name] != ""

    def string(self, name: str, *, default: Optional[str] = None, max_length: int = 1000) -> Optional[str]:
        if not self._present(name):
            return default

        value = self._raw[name]
        if not isinstance(value, str):
            raise ToolError(_("`%(name)s` must be a string.") % {"name": name})
        if len(value) > max_length:
            raise ToolError(_("`%(name)s` must be at most %(max)d characters.") % {"name": name, "max": max_length})
        return value.strip()

    def boolean(self, name: str, *, default: Optional[bool] = None) -> Optional[bool]:
        if name not in self._raw or self._raw[name] is None:
            return default

        value = self._raw[name]
        if isinstance(value, bool):
            return value
        # Models frequently send the JSON string "true" instead of the literal.
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ToolError(_("`%(name)s` must be a boolean.") % {"name": name})

    def integer(self, name: str, *, default: Optional[int], minimum: int, maximum: int) -> Optional[int]:
        if not self._present(name):
            return default

        value = self._raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ToolError(_("`%(name)s` must be an integer.") % {"name": name})
        try:
            value = int(value)
        except ValueError:
            raise ToolError(_("`%(name)s` must be an integer.") % {"name": name})

        if not minimum <= value <= maximum:
            raise ToolError(
                _("`%(name)s` must be between %(min)d and %(max)d.") % {"name": name, "min": minimum, "max": maximum}
            )
        return value

    def uuid(self, name: str, *, required: bool = False) -> Optional[str]:
        if not self._present(name):
            if required:
                raise ToolError(_("`%(name)s` is required.") % {"name": name})
            return None

        value = self._raw[name]
        if not isinstance(value, str):
            raise ToolError(_("`%(name)s` must be a UUID string.") % {"name": name})
        try:
            return str(uuid_module.UUID(value))
        except ValueError:
            raise ToolError(_("`%(name)s` is not a valid UUID: %(value)s") % {"name": name, "value": value})

    def uuid_sequence(self, name: str) -> Optional[List[str]]:
        """Return a list of UUID strings — the form a `ModelMultipleChoiceField` accepts."""
        values = self._sequence(name)
        if values is None:
            return None

        resolved = []
        for value in values:
            try:
                resolved.append(str(uuid_module.UUID(str(value))))
            except ValueError:
                raise ToolError(_("`%(name)s` contains an invalid UUID: %(value)s") % {"name": name, "value": value})
        return resolved

    def uuid_list(self, name: str) -> Optional[str]:
        """Return a comma-separated UUID string, the form the API filters accept."""
        resolved = self.uuid_sequence(name)
        if not resolved:
            return None
        return ",".join(resolved)

    def string_list(self, name: str) -> Optional[str]:
        """Return a comma-separated string of free-form tokens, as the filters expect."""
        values = self._sequence(name)
        if not values:
            return None
        return ",".join(values)

    def enum(self, name: str, choices: Sequence[str], *, default: Optional[str] = None) -> Optional[str]:
        value = self.string(name, default=default)
        if value is None:
            return None
        if value not in choices:
            raise ToolError(
                _("`%(name)s` must be one of: %(choices)s.") % {"name": name, "choices": ", ".join(choices)}
            )
        return value

    def enum_list(self, name: str, choices: Sequence[str]) -> Optional[str]:
        """Return a comma-separated string of enum members, as the filters expect."""
        values = self._sequence(name)
        if values is None:
            return None

        for value in values:
            if value not in choices:
                raise ToolError(
                    _("`%(name)s` contains an unknown value '%(value)s'. Allowed: %(choices)s.")
                    % {"name": name, "value": value, "choices": ", ".join(choices)}
                )
        return ",".join(values) or None

    def _sequence(self, name: str) -> Optional[List[str]]:
        """Accept either a JSON array or a comma-separated string for list-valued arguments."""
        if not self._present(name):
            return None

        value = self._raw[name]
        if isinstance(value, str):
            value = [token.strip() for token in value.split(",")]
        if not isinstance(value, list):
            raise ToolError(_("`%(name)s` must be an array of strings.") % {"name": name})

        return [str(item).strip() for item in value if str(item).strip()]


__all__ = ["Arguments"]
