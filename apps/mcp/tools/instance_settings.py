"""Instance settings (#20): read and change the keys the code registers. **Superusers only.**

Settings apply to every reader of every catalog (the default content policy, the
contact address), so `manage` on a catalog does not confer them. Each key carries
its JSON Schema; a write is validated against it and refused with the schema path.
"""

import logging

from django.utils.translation import gettext as _

from apps.core import settings_store
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError
from apps.mcp.registry import ToolAccess, registry

logger = logging.getLogger("apps.mcp.audit")

SETTING_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "schema": {"type": "object", "description": "JSON Schema a value must match."},
        "description": {"type": "string"},
        "default": {"description": "Built-in default."},
        "fallback": {"type": ["string", "null"], "description": "Django setting used when nothing is stored."},
        "value": {"description": "Effective value."},
        "source": {"type": "string", "enum": ["stored", "setting", "default"]},
    },
    "required": ["key", "schema", "description", "value", "source"],
}


def _require_superuser(request) -> None:
    if not request.user.is_superuser:
        raise ToolError(
            _(
                "Instance settings are restricted to administrators (superusers). They apply to every reader of "
                "every catalog; `manage` access on a catalog does not confer them."
            )
        )


def _definition(key: str):
    try:
        return settings_store.definition(key)
    except settings_store.SettingError as error:
        raise ToolError(error.message) from error


@registry.tool(
    name="get_setting",
    title="Get instance settings",
    description=(
        "Read instance settings: with `key`, that one setting; without, every setting the catalog declares. "
        "Each comes with its JSON `schema`, `description`, built-in `default`, the Django setting it falls "
        "back to, the effective `value` and its `source` (`stored` in the database, a deployment `setting`, "
        "or the `default`). **Restricted to administrators.** Call it before `update_setting`."
    ),
    input_schema={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "A dotted key, e.g. `content.default_policy`."}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"settings": {"type": "array", "items": SETTING_SCHEMA}},
        "required": ["settings"],
    },
    access=ToolAccess.USER,
)
def get_setting(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("key",))
    _require_superuser(request)
    key = arguments.string("key", max_length=100)
    if key:
        _definition(key)
        return {"settings": [settings_store.describe(key)]}
    return {"settings": settings_store.describe_all()}


@registry.tool(
    name="update_setting",
    title="Update an instance setting",
    description=(
        "Change one instance setting. `value` must match the key's `schema` (see `get_setting`); `null` "
        "deletes the stored value so the key inherits the deployment setting or the default again. Run with "
        "`dry_run: true` first to validate and see `previous` → `value`. **Restricted to administrators.** "
        "Applies to the next request."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"description": "The new value (any JSON type the key's schema allows), or null to reset."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["key", "value"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            **SETTING_SCHEMA["properties"],
            "changed": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "previous": {"description": "Effective value before the change."},
        },
        "required": ["key", "changed", "dry_run", "previous", "value", "source"],
    },
    access=ToolAccess.WRITE,
)
def update_setting(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("key", "value", "dry_run"))
    _require_superuser(request)
    key = arguments.string("key", max_length=100)
    if not key:
        raise ToolError(_("`key` is required."))
    if not arguments.sent("value"):
        raise ToolError(_("`value` is required; pass null to reset the key to its inherited value."))
    _definition(key)
    dry_run = arguments.boolean("dry_run", default=False)
    try:
        result = settings_store.update(key, arguments.json("value"), user=request.user, dry_run=dry_run)
    except settings_store.SettingError as error:
        message = error.message + (f" (at `{error.path}`, rule `{error.schema_path}`)" if error.schema_path else "")
        raise ToolError(message) from error
    if result["changed"] and not dry_run:
        logger.info("mcp.write user=%s action=update_setting key=%s", request.user.pk, key)
    return {**settings_store.describe(key), **result}
