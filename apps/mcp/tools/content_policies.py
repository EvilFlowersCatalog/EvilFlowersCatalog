"""Content policies (#174): which tier of a publication's content each reader and agent may see.

A policy is a versioned document: the highest tier per *basis* of access (open,
loan, past loan, none), caps that lower it (outside the IP ranges, an external
model provider, a TDM reservation, model-written text), which loan facts count,
a reading budget and log retention. Resolution: entry → catalog → instance
setting `content.default_policy` → `parity` (today's rules).

Superusers create and edit policies (`update_content_policy`); a catalog `manage`
user assigns existing ones (`assign_content_policy`). `explain_access` shows the
gate's decision for one entry step by step.
"""

import logging

from django.utils.translation import gettext as _

from apps.content import policies, services
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA
from apps.mcp.tools.common import assert_within_bulk_limit

logger = logging.getLogger("apps.mcp.audit")

POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "version": {"type": "integer"},
        "ref": {"type": "string", "description": "`key@version`, as receipts pin it."},
        "title": {"type": "string"},
        "builtin": {"type": "boolean", "description": "The code's own `parity`, used when the database has none."},
        "document": {"type": "object"},
    },
    "required": ["key", "version", "ref", "title", "document"],
}


def _raise(error: Exception):
    if isinstance(error, services.Refused):
        raise ToolError(error.message) from error
    detail = f" (at `{error.path}`, rule `{error.schema_path}`)" if getattr(error, "schema_path", "") else ""
    raise ToolError(error.message + detail) from error


@registry.tool(
    name="list_content_policies",
    title="List content policies",
    description=(
        "Every content policy at its current version, with its full document. A policy decides how much of a "
        "publication's text a reader or agent may see (`metadata < outline < snippet < passage < full`) for "
        "each basis of access — `open` (no Readium LCP), `loan`, `past_loan`, `none` — lowered by `caps` "
        "(outside the IP ranges, an `external` model provider, TDM reservation, model-written text). Pass "
        "`include_schema: true` for the JSON Schema a document must match."
    ),
    input_schema={
        "type": "object",
        "properties": {"include_schema": {"type": "boolean", "default": False}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "policies": {"type": "array", "items": POLICY_SCHEMA},
            "default_policy": {"type": "string", "description": "The instance default (`content.default_policy`)."},
            "schema": {"type": "object"},
        },
        "required": ["policies", "default_policy"],
    },
    access=ToolAccess.USER,
)
def list_content_policies(request, raw_arguments: dict) -> dict:
    from apps.core import settings_store

    arguments = Arguments(raw_arguments, allowed=("include_schema",))
    result = {
        "policies": [services.policy_payload(policy) for policy in policies.all_current()],
        "default_policy": settings_store.get("content.default_policy"),
    }
    if arguments.boolean("include_schema", default=False):
        result["schema"] = policies.SCHEMA
    return result


@registry.tool(
    name="get_content_policy",
    title="Get a content policy",
    description=(
        "One content policy: its current version, or with `version` an older one (every save is a new "
        "version, and receipts pin `key@version`). `include_versions: true` lists the version history."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "version": {"type": "integer", "minimum": 0},
            "include_versions": {"type": "boolean", "default": False},
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"policy": POLICY_SCHEMA, "versions": {"type": "array", "items": {"type": "object"}}},
        "required": ["policy"],
    },
    access=ToolAccess.USER,
)
def get_content_policy(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("key", "version", "include_versions"))
    key = arguments.string("key", max_length=64)
    if not key:
        raise ToolError(_("`key` is required."))
    number = arguments.integer("version", default=None, minimum=0, maximum=1_000_000)
    policy = policies.version(key, number) if number is not None else policies.current(key)
    if policy is None:
        raise ToolNotFound("Content policy", f"{key}@{number}" if number is not None else key)
    result = {"policy": services.policy_payload(policy)}
    if arguments.boolean("include_versions", default=False):
        result["versions"] = policies.versions(key)
    return result


@registry.tool(
    name="update_content_policy",
    title="Create or update a content policy",
    description=(
        "Save a content policy document as the next version of `key` (version 1 creates it). It takes effect "
        "immediately for every catalog and entry using the policy. Pass the **whole** document — start from "
        "`get_content_policy` and change what you need; `list_content_policies(include_schema: true)` has the "
        "schema. A refused document names the path of the error. Run with `dry_run: true` first. "
        "**Restricted to administrators.**"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "pattern": policies.KEY_PATTERN},
            "document": {"type": "object", "description": "The full policy document; its `key` must equal `key`."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["key", "document"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "policy": POLICY_SCHEMA,
            "changed": {"type": "boolean"},
            "created": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
        },
        "required": ["policy", "changed", "created", "dry_run"],
    },
    access=ToolAccess.WRITE,
)
def update_content_policy(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("key", "document", "dry_run"))
    try:
        services.require_superuser(request.user)
    except services.Refused as error:
        _raise(error)
    key = arguments.string("key", max_length=64)
    if not key:
        raise ToolError(_("`key` is required."))
    dry_run = arguments.boolean("dry_run", default=False)
    created = not policies.exists(key) or policies.current(key).version == 0
    try:
        policy, changed = policies.save(key, arguments.json("document"), user=request.user, dry_run=dry_run)
    except policies.PolicyError as error:
        _raise(error)
    if changed and not dry_run:
        logger.info("mcp.write user=%s action=update_content_policy policy=%s", request.user.pk, policy.ref)
    return {"policy": services.policy_payload(policy), "changed": changed, "created": created, "dry_run": dry_run}


@registry.tool(
    name="assign_content_policy",
    title="Assign a content policy",
    description=(
        "Make a catalog and/or some of its entries use content policy `key`; `key: null` clears the assignment "
        "so they inherit (entry → catalog → instance default). Needs `manage` on every catalog involved. "
        "All or nothing. Check the effect with `explain_access`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": ["string", "null"], "description": "A policy key from `list_content_policies`."},
            "catalog_id": UUID_SCHEMA,
            "entry_ids": {"type": "array", "items": UUID_SCHEMA},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "policy": {"type": ["string", "null"]},
            "catalog": {"type": ["object", "null"]},
            "entries": {"type": "array", "items": {"type": "object"}},
            "dry_run": {"type": "boolean"},
        },
        "required": ["policy", "entries", "dry_run"],
    },
    access=ToolAccess.WRITE,
)
def assign_content_policy(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("key", "catalog_id", "entry_ids", "dry_run"))
    if not arguments.sent("key"):
        raise ToolError(_("`key` is required; pass null to clear the assignment."))
    key = arguments.string("key", max_length=64)
    entry_ids = arguments.uuid_sequence("entry_ids") or []
    if entry_ids:
        entry_ids = assert_within_bulk_limit("entry_ids", entry_ids)
    dry_run = arguments.boolean("dry_run", default=False)
    try:
        result = services.assign(
            request, key or None, catalog_id=arguments.uuid("catalog_id"), entry_ids=entry_ids, dry_run=dry_run
        )
    except (services.Refused, policies.PolicyError) as error:
        _raise(error)
    if not dry_run:
        logger.info(
            "mcp.write user=%s action=assign_content_policy policy=%s entries=%s",
            request.user.pk,
            key,
            len(entry_ids),
        )
    return {**result.payload(), "dry_run": dry_run}


@registry.tool(
    name="explain_access",
    title="Explain content access",
    description=(
        "Why the content gate decides what it does for one publication: the basis of access, the policy "
        "`key@version` and where it was assigned, each cap applied and the final tier "
        "(`metadata < outline < snippet < passage < full`). `user_id` explains for another reader "
        "(administrators; your IP address stands in for theirs). `processor` (`local`/`external`) shows what a "
        "model provider of that kind would get; `policy` what another policy would decide, without assigning it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry_id": UUID_SCHEMA,
            "user_id": UUID_SCHEMA,
            "processor": {"type": "string", "enum": ["local", "external"]},
            "policy": {"type": "string", "description": "A policy key to try instead of the assigned one."},
        },
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "tier": {"type": ["string", "null"]},
            "basis": {"type": ["string", "null"]},
            "reason": {"type": ["string", "null"]},
            "how_to_unlock": {"type": "array", "items": {"type": "string"}},
            "policy": {"type": "string"},
            "policy_source": {"type": "string"},
            "caps": {"type": "array", "items": {"type": "object"}},
            "visible": {"type": "boolean"},
        },
        "required": ["tier", "basis", "policy", "caps", "visible"],
    },
    access=ToolAccess.USER,
)
def explain_access(request, raw_arguments: dict) -> dict:
    from apps.content.access import Surface

    arguments = Arguments(raw_arguments, allowed=("entry_id", "user_id", "processor", "policy"))
    entry_id = arguments.uuid("entry_id", required=True)
    try:
        return services.explain(
            request,
            entry_id,
            user_id=arguments.uuid("user_id"),
            surface=Surface.MCP,
            processor=arguments.enum("processor", ("local", "external")),
            policy_key=arguments.string("policy", max_length=64),
        )
    except (services.Refused, policies.PolicyError) as error:
        _raise(error)
