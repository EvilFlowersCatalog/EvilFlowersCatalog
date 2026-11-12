"""User accounts: who exists, what they may reach, and the lifecycle around that.

These mirror `apps/api/views/users.py` and reuse its permission model —
`core.view_user`, `core.add_user`, `core.change_user`, `core.delete_user` —
with four rules the REST endpoints do not enforce, because an agent holding a
credential is exactly the caller they exist to stop:

1. **Not-found is the only refusal for a record you cannot see.** Every lookup
   goes through `UserFilter` (everyone for `core.view_user`, yourself
   otherwise). REST answers 404 for a missing id and 403 for someone else's,
   which tells an ordinary user which ids are real accounts.
2. **Only a superuser may touch a superuser.** `core.change_user` alone would
   otherwise let a user administrator reset an administrator's password or
   deactivate them — a privilege escalation in two calls.
3. **Nobody changes their own role, active flag or password here, or deletes
   themselves.** The first two lock people out; the last turns an API key into
   a way to take over the account's password login.
4. **Catalog memberships are scoped to what the caller can read.** Reporting
   another user's access list unfiltered would disclose private catalogs.

Granting catalog access stays a portal action, as it does for `update_catalog`.
"""

import logging

from django.db import transaction
from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.api.filters.catalogs import CatalogFilter
from apps.api.filters.users import UserFilter
from apps.api.forms.users import UserForm
from apps.core.models import ApiKey, AuthSource, Catalog, User, UserCatalog
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolError, ToolNotFound, ToolValidationError
from apps.mcp.pagination import PAGINATION_ARGUMENTS, paginate, pagination_schema, read_pagination
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import DELETION_OUTPUT_SCHEMA, UUID_SCHEMA, list_output, single_output

logger = logging.getLogger("apps.mcp.audit")

USER_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "username": {"type": "string"},
        "name": {"type": "string"},
        "surname": {"type": "string"},
        "full_name": {"type": "string"},
        "is_active": {"type": "boolean"},
        "is_superuser": {"type": "boolean", "description": "Administrator role."},
        "auth_source": {
            "type": "string",
            "description": "Where the password lives: 'database' or 'ldap'. LDAP profiles are refreshed at login.",
        },
        "has_password": {
            "type": "boolean",
            "description": "False for API-key-only service accounts and LDAP users (no local password).",
        },
        "last_login": {"type": ["string", "null"], "format": "date-time"},
        "created_at": {"type": "string", "format": "date-time"},
    },
    "required": ["id", "username", "is_active", "is_superuser"],
}

USER_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        **USER_SCHEMA["properties"],
        "catalogs": {
            "type": "array",
            "description": "Explicit catalog grants, limited to catalogs the caller can read.",
            "items": {
                "type": "object",
                "properties": {
                    "catalog_id": UUID_SCHEMA,
                    "title": {"type": "string"},
                    "url_name": {"type": "string"},
                    "mode": {"type": "string", "enum": list(UserCatalog.Mode.values)},
                },
                "required": ["catalog_id", "mode"],
            },
        },
        "active_api_keys": {
            "type": "integer",
            "description": "Present only when the caller may see this user's API keys (their own, or a superuser).",
        },
        "lcp_passphrase_hint": {"type": ["string", "null"]},
    },
    "required": USER_SCHEMA["required"],
}

USER_FIELDS = {
    "name": {"type": "string", "maxLength": 30, "description": "First name."},
    "surname": {"type": "string", "maxLength": 150},
    "is_active": {
        "type": "boolean",
        "description": "False blocks every login and every API key of the account immediately.",
    },
    "is_superuser": {
        "type": "boolean",
        "description": "Administrator role. Only a superuser may grant or revoke it, and never on themselves.",
    },
    "lcp_passphrase_hint": {
        "type": "string",
        "maxLength": 255,
        "description": "Hint shown by Readium readers when they ask for the LCP passphrase.",
    },
}


# -- authorisation -------------------------------------------------------


def _visible_users(request):
    return UserFilter(QueryDict(mutable=True), queryset=User.objects.select_related("auth_source"), request=request).qs


def _resolve_visible_user(request, user_id: str) -> User:
    """The user, if this credential may see them. Otherwise `ToolNotFound` — never 403."""
    user = _visible_users(request).filter(pk=user_id).first()
    if user is None:
        raise ToolNotFound("User", user_id)
    return user


def _require_permission(request, permission: str, action: str) -> None:
    if not request.user.has_perm(permission):
        raise ToolError(
            _(
                "%(action)s is restricted to administrators (it needs the `%(permission)s` permission). "
                "`manage` access on a catalog does not confer it."
            )
            % {"action": action, "permission": permission}
        )


def _assert_may_administer(request, target: User, action: str) -> None:
    """Rule 2: a user administrator who is not a superuser may not act on one."""
    if target.is_superuser and not request.user.is_superuser:
        raise ToolError(
            _(
                "This is an administrator account: to %(action)s it is restricted to administrators "
                "with superuser rights."
            )
            % {"action": action}
        )


# -- projections ---------------------------------------------------------


def _user(instance: User) -> dict:
    return {
        "id": str(instance.pk),
        "username": instance.username,
        "name": instance.name,
        "surname": instance.surname,
        "full_name": instance.full_name,
        "is_active": instance.is_active,
        "is_superuser": instance.is_superuser,
        "auth_source": instance.auth_source.driver,
        "has_password": instance.has_usable_password(),
        "last_login": instance.last_login.isoformat() if instance.last_login else None,
        "created_at": instance.created_at.isoformat(),
    }


def _user_detail(request, instance: User) -> dict:
    grants = UserCatalog.objects.filter(user=instance).select_related("catalog").order_by("catalog__title")

    # Rule 4. A superuser reads every catalog, and so does the subject for
    # their own grants; anyone else sees only grants on catalogs they can read.
    if not (request.user.is_superuser or instance.pk == request.user.pk):
        readable = CatalogFilter(QueryDict(mutable=True), queryset=Catalog.objects.all(), request=request).qs
        grants = grants.filter(catalog__in=readable)

    payload = {
        **_user(instance),
        "lcp_passphrase_hint": instance.lcp_passphrase_hint,
        "catalogs": [
            {
                "catalog_id": str(grant.catalog_id),
                "title": grant.catalog.title,
                "url_name": grant.catalog.url_name,
                "mode": grant.mode,
            }
            for grant in grants
        ],
    }

    # Same boundary as `ApiKeyFilter`: your own keys, or everyone's for a superuser.
    if request.user.is_superuser or instance.pk == request.user.pk:
        payload["active_api_keys"] = ApiKey.objects.filter(user=instance, is_active=True).count()

    return payload


def _validated_profile(payload: dict) -> dict:
    """Run the REST `UserForm` over the profile fields and return what it cleaned."""
    form = UserForm({key: value for key, value in payload.items() if value is not None})
    if not form.is_valid():
        raise ToolValidationError(form)
    return form.cleaned_data


# -- tools ---------------------------------------------------------------


@registry.tool(
    name="list_users",
    title="List users",
    description=(
        "List user accounts. Administrators (the `core.view_user` permission) see everyone; any other "
        "credential sees only its own account, so an empty or single-row result is a permission boundary, "
        "not proof that nobody else exists. `query` matches username, first name and surname at once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words that must all match username, name or surname."},
            "is_active": {"type": "boolean"},
            "is_superuser": {"type": "boolean", "description": "True returns administrators only."},
            "catalog_id": {**UUID_SCHEMA, "description": "Only users with an explicit grant on this catalog."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(USER_SCHEMA),
    access=ToolAccess.USER,
)
def list_users(request, raw_arguments: dict) -> dict:
    arguments = Arguments(
        raw_arguments, allowed=("query", "is_active", "is_superuser", "catalog_id", *PAGINATION_ARGUMENTS)
    )
    requested = read_pagination(arguments)

    params = QueryDict(mutable=True)
    for key, value in (
        ("query", arguments.string("query", max_length=200)),
        ("catalog_id", arguments.uuid("catalog_id")),
    ):
        if value:
            params[key] = value
    for flag in ("is_active", "is_superuser"):
        value = arguments.boolean(flag)
        if value is not None:
            params[flag] = "true" if value else "false"

    users = UserFilter(params, queryset=User.objects.select_related("auth_source"), request=request).qs
    page = paginate(users.distinct().order_by("username"), requested)

    return {"items": [_user(item) for item in page.items], "metadata": page.metadata()}


@registry.tool(
    name="get_user",
    title="Get user details",
    description=(
        "Fetch one user account with its catalog grants. Without `user_id` returns your own account. "
        "Another user's account is visible only to administrators (`core.view_user`); otherwise the "
        "answer is 'does not exist'. Catalog grants are limited to catalogs you can read yourself."
    ),
    input_schema={
        "type": "object",
        "properties": {"user_id": {**UUID_SCHEMA, "description": "Defaults to the calling user."}},
        "additionalProperties": False,
    },
    output_schema=single_output("user", USER_DETAIL_SCHEMA),
    access=ToolAccess.USER,
)
def get_user(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("user_id",))
    user_id = arguments.uuid("user_id") or str(request.user.pk)

    return {"user": _user_detail(request, _resolve_visible_user(request, user_id))}


@registry.tool(
    name="create_user",
    title="Create a user",
    description=(
        "Create a local (database) user account. **Restricted to administrators** (`core.add_user`). "
        "`password` is optional: leave it out to create an API-key-only service account, then call "
        "`create_api_key` with its `user_id` — a password sent here passes through the conversation, "
        "so prefer that route for agents and let people set their own password in the portal. "
        "Usernames are stored lower-case, matching how login compares them. Only a superuser may "
        "create another administrator. The account starts with no catalog access; grant it in the portal."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "username": {"type": "string", "maxLength": 200, "description": "Login name, unique; stored lower-case."},
            "name": USER_FIELDS["name"],
            "surname": USER_FIELDS["surname"],
            "password": {
                "type": "string",
                "minLength": 8,
                "maxLength": 128,
                "description": "Omit for an account that can authenticate with API keys only.",
            },
            "is_active": {**USER_FIELDS["is_active"], "default": True},
            "is_superuser": {**USER_FIELDS["is_superuser"], "default": False},
        },
        "required": ["username", "name", "surname"],
        "additionalProperties": False,
    },
    output_schema=single_output("user", USER_DETAIL_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
@transaction.atomic
def create_user(request, raw_arguments: dict) -> dict:
    arguments = Arguments(
        raw_arguments, allowed=("username", "name", "surname", "password", "is_active", "is_superuser")
    )
    # Before any lookup, so a refusal says nothing about which usernames exist.
    _require_permission(request, "core.add_user", _("Creating users"))

    is_superuser = arguments.boolean("is_superuser", default=False)
    if is_superuser and not request.user.is_superuser:
        raise ToolError(_("Granting the administrator role is restricted to administrators with superuser rights."))

    username = (arguments.string("username", max_length=200) or "").lower()
    if not username:
        raise ToolError(_("`username` is required."))

    password = arguments.string("password", max_length=128)
    if password is not None and len(password) < 8:
        raise ToolError(_("`password` must be at least 8 characters, or omitted for an API-key-only account."))

    profile = _validated_profile(
        {
            "name": arguments.string("name", max_length=30),
            "surname": arguments.string("surname", max_length=150),
            "is_active": arguments.boolean("is_active", default=True),
        }
    )

    # Usernames are unique deployment-wide, including accounts this caller
    # cannot list — but only a `core.add_user` holder gets this far.
    if User.objects.filter(username__iexact=username).exists():
        raise ToolConflict(_("A user named '%(username)s' already exists.") % {"username": username})

    auth_source = AuthSource.objects.filter(driver=AuthSource.Driver.DATABASE, is_active=True).first()
    if auth_source is None:
        raise ToolError(_("This deployment has no active database auth source, so local accounts cannot be created."))

    user = User(
        username=username,
        name=profile["name"],
        surname=profile["surname"],
        is_active=profile.get("is_active", True),
        is_superuser=is_superuser,
        auth_source=auth_source,
    )
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()
    user.save()

    logger.info(
        "mcp.write user=%s action=create_user target=%s superuser=%s password=%s",
        request.user.pk,
        user.pk,
        is_superuser,
        bool(password),
    )
    return {"user": _user_detail(request, user)}


@registry.tool(
    name="update_user",
    title="Update a user",
    description=(
        "Change a user's profile (name, surname, LCP passphrase hint), active flag, administrator role "
        "or password. Only the arguments you pass are changed. You may edit your own profile fields; "
        "everything else, and anyone else's account, needs `core.change_user`. Only a superuser may "
        "change an administrator's account or anyone's role. Nobody may change their own role, active "
        "flag or password here. LDAP users get their name re-synced from the directory at next login."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {**UUID_SCHEMA, "description": "Account to change."},
            **USER_FIELDS,
            "password": {
                "type": "string",
                "minLength": 8,
                "maxLength": 128,
                "description": "New password for a database account. Not accepted for your own account.",
            },
        },
        "required": ["user_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("user", USER_DETAIL_SCHEMA),
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def update_user(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("user_id", *USER_FIELDS, "password"))
    target = _resolve_visible_user(request, arguments.uuid("user_id", required=True))

    changes = _requested_changes(arguments, target)
    _authorize_update(request, target, changes)

    for field, value in changes.items():
        if field == "password":
            target.set_password(value)
        else:
            setattr(target, field, value)
    target.save()

    logger.info(
        "mcp.write user=%s action=update_user target=%s fields=%s", request.user.pk, target.pk, ",".join(changes)
    )
    return {"user": _user_detail(request, target)}


def _requested_changes(arguments: Arguments, target: User) -> dict:
    """Only the fields whose value would actually change; `password` whenever given."""
    hint = arguments.string("lcp_passphrase_hint", max_length=255)
    profile = _validated_profile(
        {
            "name": arguments.string("name", max_length=30) or target.name,
            "surname": arguments.string("surname", max_length=150) or target.surname,
            "lcp_passphrase_hint": hint,
        }
    )
    requested = {
        "name": profile["name"],
        "surname": profile["surname"],
        "lcp_passphrase_hint": profile.get("lcp_passphrase_hint") if hint is not None else None,
        "is_active": arguments.boolean("is_active"),
        "is_superuser": arguments.boolean("is_superuser"),
    }
    changes = {
        field: value for field, value in requested.items() if value is not None and value != getattr(target, field)
    }

    password = arguments.string("password", max_length=128)
    if password is not None:
        changes["password"] = password
    return changes


def _authorize_update(request, target: User, changes: dict) -> None:
    """Rules 2 and 3 of the module docstring, applied to what the call would change."""
    is_self = target.pk == request.user.pk

    if not is_self:
        _require_permission(request, "core.change_user", _("Changing another user's account"))
        _assert_may_administer(request, target, _("change"))

    if "is_superuser" in changes and not request.user.is_superuser:
        raise ToolError(_("Changing roles is restricted to administrators with superuser rights."))
    if is_self and "is_superuser" in changes:
        raise ToolError(_("You cannot change your own role."))
    if is_self and "is_active" in changes:
        raise ToolError(_("You cannot activate or deactivate your own account."))

    if "password" not in changes:
        return
    if is_self:
        raise ToolError(_("Change your own password in the portal, not through an agent session."))
    if len(changes["password"]) < 8:
        raise ToolError(_("`password` must be at least 8 characters."))
    if target.auth_source.driver != AuthSource.Driver.DATABASE:
        raise ToolError(_("This account authenticates against LDAP; its password is managed by the directory."))


@registry.tool(
    name="delete_user",
    title="Delete a user",
    description=(
        "Permanently delete a user account **and everything attached to it** — API keys, shelf, "
        "annotations, Readium loans and catalog grants. Cannot be undone; prefer `update_user` with "
        "`is_active: false`, which blocks the account and keeps its history. **Restricted to "
        "administrators** (`core.delete_user`); only a superuser may delete an administrator, and "
        "nobody may delete themselves. Pass `confirm_username` matching the account's username."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {**UUID_SCHEMA, "description": "Account to delete."},
            "confirm_username": {"type": "string", "description": "The account's exact username, as confirmation."},
        },
        "required": ["user_id", "confirm_username"],
        "additionalProperties": False,
    },
    output_schema={
        **DELETION_OUTPUT_SCHEMA,
        "properties": {**DELETION_OUTPUT_SCHEMA["properties"], "username": {"type": "string"}},
    },
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def delete_user(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("user_id", "confirm_username"))
    _require_permission(request, "core.delete_user", _("Deleting users"))

    target = _resolve_visible_user(request, arguments.uuid("user_id", required=True))
    if target.pk == request.user.pk:
        raise ToolError(_("You cannot delete your own account."))
    _assert_may_administer(request, target, _("delete"))

    if arguments.string("confirm_username", max_length=200) != target.username:
        raise ToolError(
            _(
                "`confirm_username` does not match. To delete this account pass its exact username, "
                "'%(username)s'. Nothing has been deleted."
            )
            % {"username": target.username}
        )

    identifier, username = str(target.pk), target.username
    target.delete()

    logger.info("mcp.write user=%s action=delete_user target=%s", request.user.pk, identifier)
    return {"deleted": True, "id": identifier, "username": username}


__all__ = ["create_user", "delete_user", "get_user", "list_users", "update_user"]
