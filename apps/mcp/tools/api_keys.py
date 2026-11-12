"""API keys: the long-lived Bearer credentials agents and integrations use.

Mirrors `apps/api/views/api_keys.py` — any authenticated user may mint keys for
themselves, only a superuser may mint one for somebody else (which is
impersonation, and logged as such) — with two deliberate differences:

* **A token is shown once.** `ApiKeySerializer` recomputes the JWT on every
  read, so the REST listing hands out every key's token again to whoever can
  list it. Here `create_api_key` is the only tool that returns a token; the
  listing returns metadata only.
* **Revoking deactivates rather than deletes.** `BearerBackend` already refuses
  an inactive key, and keeping the row keeps `last_seen_at`, which is what
  you want when investigating a leaked key.

Visibility is `ApiKeyFilter`'s: your own keys, or every key for a superuser.
A key you cannot see is reported as not existing.
"""

import logging

from django.db import transaction
from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.api.filters.api_keys import ApiKeyFilter
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, User
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.pagination import PAGINATION_ARGUMENTS, paginate, pagination_schema, read_pagination
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA, list_output, single_output

logger = logging.getLogger("apps.mcp.audit")

#: `ApiKeyForm.name` — the model allows 200, REST accepts 100.
NAME_MAX_LENGTH = 100

API_KEY_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "user_id": UUID_SCHEMA,
        "username": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "is_active": {"type": "boolean"},
        "is_current": {
            "type": "boolean",
            "description": "True for the key authenticating this very session.",
        },
        "last_seen_at": {"type": ["string", "null"], "format": "date-time"},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": ["id", "user_id", "is_active", "is_current"],
}


def _current_key_id(request):
    # Set by `BearerBackend` for API-key sessions only; absent for Basic auth.
    api_key = getattr(request, "api_key", None)
    return api_key.pk if api_key is not None else None


def _api_key(request, instance: ApiKey) -> dict:
    return {
        "id": str(instance.pk),
        "user_id": str(instance.user_id),
        "username": instance.user.username,
        "name": instance.name,
        "is_active": instance.is_active,
        "is_current": instance.pk == _current_key_id(request),
        "last_seen_at": instance.last_seen_at.isoformat() if instance.last_seen_at else None,
        "created_at": instance.created_at.isoformat(),
    }


def _visible_keys(request, params: QueryDict = None):
    return ApiKeyFilter(
        params if params is not None else QueryDict(mutable=True),
        queryset=ApiKey.objects.select_related("user"),
        request=request,
    ).qs


@registry.tool(
    name="list_api_keys",
    title="List API keys",
    description=(
        "List API keys — your own, or every user's for a superuser. Returns metadata only: a key's "
        "token is shown once, by `create_api_key`, and cannot be retrieved again. `is_current` marks "
        "the key this session is using. Use it to find stale keys (`last_seen_at`) before revoking them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {
                **UUID_SCHEMA,
                "description": "Only this user's keys (superusers; others only see their own).",
            },
            "name": {"type": "string", "description": "Substring match on the key name."},
            "is_active": {"type": "boolean", "description": "False lists revoked keys."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(API_KEY_SCHEMA),
    access=ToolAccess.USER,
)
def list_api_keys(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("user_id", "name", "is_active", *PAGINATION_ARGUMENTS))
    requested = read_pagination(arguments)

    params = QueryDict(mutable=True)
    if arguments.uuid("user_id"):
        params["user_id"] = arguments.uuid("user_id")
    if arguments.string("name", max_length=NAME_MAX_LENGTH):
        params["name"] = arguments.string("name", max_length=NAME_MAX_LENGTH)

    keys = _visible_keys(request, params)
    is_active = arguments.boolean("is_active")
    if is_active is not None:
        keys = keys.filter(is_active=is_active)

    page = paginate(keys.order_by("-created_at"), requested)
    return {"items": [_api_key(request, item) for item in page.items], "metadata": page.metadata()}


@registry.tool(
    name="create_api_key",
    title="Create an API key",
    description=(
        "Mint a new API key and return its Bearer `token`. **The token is shown exactly once** — hand it "
        "to the user or write it into the client's configuration now; it cannot be listed again. "
        "The key acts with the full permissions of the account it belongs to, until revoked with "
        "`revoke_api_key`. By default it belongs to you. Passing `user_id` mints a key for someone "
        "else, which lets the holder act as that user, so it is **restricted to superusers**; use it "
        "with an API-key-only account from `create_user` to provision an agent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "maxLength": NAME_MAX_LENGTH,
                "description": "What the key is for, e.g. 'Claude Desktop – laptop'. Needed to revoke the right one later.",
            },
            "user_id": {
                **UUID_SCHEMA,
                "description": "Owner of the key. Defaults to you; anyone else needs superuser.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    output_schema=single_output(
        "api_key",
        API_KEY_SCHEMA,
        extra={
            "token": {"type": "string", "description": "The Bearer credential. Shown once."},
            "authorization_header": {"type": "string"},
        },
    ),
    access=ToolAccess.WRITE,
    idempotent=False,
)
@transaction.atomic
def create_api_key(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("name", "user_id"))

    owner_id = arguments.uuid("user_id")
    if owner_id and owner_id != str(request.user.pk):
        # Checked before the lookup, so a refusal says nothing about the account.
        if not request.user.is_superuser:
            raise ToolError(
                _(
                    "Creating an API key for another user is restricted to administrators with superuser "
                    "rights — the key would let its holder act as that user. Omit `user_id` to create "
                    "one for yourself."
                )
            )
        owner = User.objects.filter(pk=owner_id).first()
        if owner is None:
            raise ToolNotFound("User", owner_id)
    else:
        owner = request.user

    name = arguments.string("name", max_length=NAME_MAX_LENGTH)
    if not name:
        raise ToolError(_("`name` is required — it is how the key is told apart when revoking it."))

    if not owner.is_active:
        raise ToolError(
            _("'%(username)s' is deactivated; a key for it would never authenticate.") % {"username": owner.username}
        )

    api_key = ApiKey.objects.create(user=owner, name=name)
    token = JWTFactory(str(owner.pk)).api_key(str(api_key.pk))

    # Never the token itself.
    logger.info(
        "mcp.write user=%s action=create_api_key api_key=%s owner=%s impersonation=%s",
        request.user.pk,
        api_key.pk,
        owner.pk,
        owner.pk != request.user.pk,
    )
    return {
        "api_key": _api_key(request, api_key),
        "token": token,
        "authorization_header": f"Bearer {token}",
    }


@registry.tool(
    name="revoke_api_key",
    title="Revoke an API key",
    description=(
        "Revoke an API key so it no longer authenticates. Takes effect on the next request. You may "
        "revoke your own keys; a superuser may revoke anyone's. The key is deactivated, not deleted, so "
        "it stays in `list_api_keys` (with `is_active: false`) for auditing. Revoking the key this "
        "session uses (`is_current`) ends the session's access — confirm with the user first."
    ),
    input_schema={
        "type": "object",
        "properties": {"api_key_id": {**UUID_SCHEMA, "description": "Key id, from `list_api_keys`."}},
        "required": ["api_key_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "revoked": {"type": "boolean", "description": "False when the key was already inactive."},
            "api_key": API_KEY_SCHEMA,
        },
        "required": ["revoked", "api_key"],
    },
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def revoke_api_key(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("api_key_id",))
    api_key_id = arguments.uuid("api_key_id", required=True)

    api_key = _visible_keys(request).filter(pk=api_key_id).first()
    if api_key is None:
        raise ToolNotFound("API key", api_key_id)

    revoked = api_key.is_active
    if revoked:
        api_key.is_active = False
        api_key.save(update_fields=["is_active", "updated_at"])

    logger.info(
        "mcp.write user=%s action=revoke_api_key api_key=%s owner=%s changed=%s",
        request.user.pk,
        api_key.pk,
        api_key.user_id,
        revoked,
    )
    return {"revoked": revoked, "api_key": _api_key(request, api_key)}


__all__ = ["create_api_key", "list_api_keys", "revoke_api_key"]
