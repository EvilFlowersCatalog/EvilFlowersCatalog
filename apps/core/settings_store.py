"""Instance settings a superuser changes without a deploy (#20).

Each key is declared in code — type (a JSON Schema), default, description and the
`EVILFLOWERS_*` Django setting it falls back to — and the database stores only the
values someone set. A value resolves in three steps, the same inheritance as the
notification branding (IP-025):

1. the `settings` row, if one is stored;
2. the Django setting named by `fallback`, if the deployment sets one;
3. the built-in `default`.

Keys are dotted, one namespace per app (`content.*`, `branding.*`). An app declares
its own keys with `register()` at import time; the core ones are at the bottom of
this module and `apps.content` registers its keys from `AppConfig.ready()`.

Stored values are cached in Redis as one map and dropped when a write commits, so
a read costs no query and a change applies to the next request.

REST: `/api/v1/settings` (superusers). MCP: `get_setting`, `update_setting`.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from jsonschema import Draft202012Validator, FormatChecker

logger = logging.getLogger(__name__)

CACHE_KEY = "settings:v1"
CACHE_TIMEOUT = 60 * 60

#: A sentinel for "the deployment does not set this Django setting".
_MISSING = object()


class SettingError(ValueError):
    """A write the registry refuses: unknown key, or a value that does not match the declared type."""

    def __init__(self, message: str, path: str = "", schema_path: str = ""):
        super().__init__(message)
        self.message = message
        #: Where in the value the error is (`tiers/loan`), and which schema rule refused it.
        self.path = path
        self.schema_path = schema_path


def schema_errors(schema: dict, value: Any, prefix: str = "") -> None:
    """Validate `value` against a JSON Schema; raise `SettingError` for the first error, with both paths."""
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        raise SettingError(
            f"{prefix}{error.message}",
            path="/".join(str(part) for part in error.absolute_path),
            schema_path="/".join(str(part) for part in error.absolute_schema_path),
        )


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    #: JSON Schema of the value. The API validates writes against it and the portal renders its form from it.
    schema: Dict[str, Any]
    description: str
    #: The built-in default, or a callable returning it (read at resolution time, so tests can override settings).
    default: Any = None
    #: Name of the Django setting that overrides the default when the database has no row.
    fallback: Optional[str] = None
    #: Extra check after the schema, e.g. "the content policy exists". Raises `SettingError`.
    validate: Optional[Callable[[Any], None]] = None
    #: Who may change it. Superusers only, for now (#20); kept explicit for the registry listing.
    permission: str = "superuser"
    #: Values that are secrets are never returned by the API. None of the current keys is one.
    secret: bool = False
    tags: List[str] = field(default_factory=list)

    def default_value(self) -> Any:
        return self.default() if callable(self.default) else self.default

    def inherited_value(self) -> Any:
        """What the key resolves to with no database row."""
        if self.fallback:
            value = getattr(settings, self.fallback, _MISSING)
            if value is not _MISSING and value not in (None, ""):
                return value
        return self.default_value()

    def check(self, value: Any) -> Any:
        schema_errors(self.schema, value, prefix=f"{self.key}: ")
        if self.validate is not None:
            self.validate(value)
        return value

    def describe(self) -> dict:
        return {
            "key": self.key,
            "schema": self.schema,
            "description": self.description,
            "default": self.default_value(),
            "fallback": self.fallback,
            "permission": self.permission,
        }


_registry: Dict[str, SettingDefinition] = {}


def register(definition: SettingDefinition) -> SettingDefinition:
    if definition.key in _registry and _registry[definition.key] != definition:
        raise ValueError(f"Setting {definition.key} is registered twice")
    _registry[definition.key] = definition
    return definition


def definition(key: str) -> SettingDefinition:
    try:
        return _registry[key]
    except KeyError:
        raise SettingError(f"Unknown setting '{key}'. Known: {', '.join(sorted(_registry))}.") from None


def definitions() -> List[SettingDefinition]:
    return [_registry[key] for key in sorted(_registry)]


def database_cache_key(name: str) -> str:
    """A cache key scoped to the database: a test run (`test_*`) never reads the dev server's values, nor
    another test run's — they share one Redis, and a test's rows roll back while its cache entry would not."""
    return f"{name}:{connection.settings_dict['NAME']}"


def _stored() -> Dict[str, Any]:
    """Every stored value, from the cache or with one query."""
    key = database_cache_key(CACHE_KEY)
    values = cache.get(key)
    if values is None:
        from apps.core.models import Setting

        values = dict(Setting.objects.values_list("key", "value"))
        cache.set(key, values, CACHE_TIMEOUT)
    return values


def invalidate() -> None:
    cache.delete(database_cache_key(CACHE_KEY))


def get(key: str) -> Any:
    """The effective value of a registered key."""
    item = definition(key)
    stored = _stored()
    if key in stored:
        return stored[key]
    return item.inherited_value()


def resolve(key: str) -> dict:
    """The value and where it came from: `stored`, `setting` (the Django fallback) or `default`."""
    item = definition(key)
    stored = _stored()
    if key in stored:
        return {"value": stored[key], "source": "stored"}
    return {"value": item.inherited_value(), "source": _inherited_source(item)}


def describe(key: str) -> dict:
    item = definition(key)
    resolved = resolve(key)
    payload = {**item.describe(), "value": resolved["value"], "source": resolved["source"]}
    if item.secret:
        payload["value"] = None
    return payload


def describe_all() -> List[dict]:
    return [describe(item.key) for item in definitions()]


def update(key: str, value: Any, user=None, dry_run: bool = False) -> dict:
    """Store a value, or with `value=None` delete the row so the key inherits again.

    Returns `{"key", "changed", "previous", "value", "source"}`; with `dry_run` nothing is written.
    """
    from apps.core.models import Setting

    item = definition(key)
    before = resolve(key)
    if value is not None:
        item.check(value)

    if value is None:
        changed = before["source"] == "stored"
    else:
        changed = before["source"] != "stored" or before["value"] != value

    if changed and not dry_run:
        with transaction.atomic():
            if value is None:
                Setting.objects.filter(pk=key).delete()
            else:
                Setting.objects.update_or_create(pk=key, defaults={"value": value, "updated_by": user})
            # Now, for this request's own reads, and again on commit for readers that refilled the cache
            # from the old row in between. A rolled-back write (a test) leaves at worst a stale entry that
            # the next write or the TTL drops.
            invalidate()
            transaction.on_commit(invalidate)
        logger.info("settings.update key=%s user=%s reset=%s", key, getattr(user, "pk", None), value is None)

    if not changed:
        after = before
    elif value is None:
        after = {"value": item.inherited_value(), "source": _inherited_source(item)}
    else:
        after = {"value": value, "source": "stored"}
    return {
        "key": key,
        "changed": changed,
        "dry_run": dry_run,
        "previous": before["value"],
        "value": after["value"],
        "source": after["source"],
    }


def _inherited_source(item: SettingDefinition) -> str:
    if item.fallback:
        value = getattr(settings, item.fallback, _MISSING)
        if value is not _MISSING and value not in (None, ""):
            return "setting"
    return "default"


# --- core keys ---------------------------------------------------------------------------------------------

register(
    SettingDefinition(
        key="branding.contact_email",
        schema={"type": "string", "format": "email", "maxLength": 254},
        description="The address readers are told to write to: notification e-mails, refused loans.",
        default="root@localhost",
        fallback="EVILFLOWERS_CONTACT_EMAIL",
        tags=["branding"],
    )
)

register(
    SettingDefinition(
        key="branding.logo",
        schema={
            "type": "string",
            "description": "An asset name, an absolute https URL, or a `data:image/...;base64,` URI.",
        },
        description="The library logo for the licence page and watermarks. Unlimited size: a base64 image fits.",
        default="",
        fallback="EVILFLOWERS_BRANDING_LOGO",
        tags=["branding"],
    )
)


__all__ = [
    "SettingDefinition",
    "SettingError",
    "definition",
    "definitions",
    "describe",
    "describe_all",
    "get",
    "invalidate",
    "register",
    "resolve",
    "schema_errors",
    "update",
]
