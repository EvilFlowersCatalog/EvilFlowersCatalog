"""Notification e-mails: branding, previews and the LDAP contact import (IP-025).

Every tool here is **restricted to superusers**. Branding is deployment-wide —
it changes the e-mail every reader of every catalog receives — and the LDAP
sync writes contacts of users the caller cannot otherwise see, so `manage` on
a catalog (or `core.change_user`) is not enough.

Branding is tokens, not template source: the library name, a logo, colours
and the footer. Structural changes to an e-mail are template overrides on the
IaC-mounted volume (`EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR`), which is also
where logo images live — `get_notification_branding` lists both, and the
`logo` token names one of those assets (or an absolute https URL).

No tool here ever returns an e-mail address of another user.
"""

import logging

from django.utils.translation import gettext as _

from apps.core.models import AuthSource, User
from apps.core.services.ldap_directory import DirectorySyncError, sync_users
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA
from apps.mcp.tools.common import assert_within_bulk_limit
from apps.notifications import branding
from apps.notifications.models import NotificationBranding, NotificationLog
from apps.notifications.registry import TemplateRegistry
from apps.notifications.services import NotificationService

logger = logging.getLogger("apps.mcp.audit")

COLOR_FIELDS = ("header_background", "header_text_color", "primary_color", "button_text_color", "background_color")
TEXT_LIMITS = {
    "library_name": 200,
    "logo": 500,
    "logo_alt": 200,
    "logo_link": 500,
    "footer_text": 2000,
    "website_url": 500,
    "from_name": 100,
}
TOKEN_FIELDS = NotificationBranding.TOKEN_FIELDS

COLOR_SCHEMA = {"type": "string", "pattern": "^#([0-9a-fA-F]{3}){1,2}$"}
TOKEN_SCHEMA = {
    "library_name": {"type": "string", "maxLength": 200, "description": "Header/footer name of the library."},
    "logo": {
        "type": "string",
        "maxLength": 500,
        "description": "An asset name from `assets` (served by the catalog, e.g. `stu-logo.png`) or an absolute "
        "https URL of a PNG/JPEG. SVG is refused: Gmail and Outlook do not render it.",
    },
    "logo_alt": {"type": "string", "maxLength": 200, "description": "Alt text; defaults to the library name."},
    "logo_width": {"type": "integer", "minimum": 40, "maximum": 600, "description": "Rendered width in px."},
    "logo_link": {"type": "string", "maxLength": 500, "description": "Where the logo links; defaults to website."},
    "header_background": {**COLOR_SCHEMA, "description": "Header band colour. Use white for a logo with dark text."},
    "header_text_color": {**COLOR_SCHEMA, "description": "Library name colour in the header."},
    "primary_color": {**COLOR_SCHEMA, "description": "Accent strip under the header, buttons and footer links."},
    "button_text_color": COLOR_SCHEMA,
    "background_color": {**COLOR_SCHEMA, "description": "Page background around the message."},
    "footer_text": {
        "type": "string",
        "maxLength": 2000,
        "description": "Plain text above the do-not-reply line (address, opening hours). Newlines are kept.",
    },
    "website_url": {"type": "string", "maxLength": 500, "description": "Library website, linked in the footer."},
    "from_name": {"type": "string", "maxLength": 100, "description": "Display name in the From: header."},
}

BRANDING_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "effective": {
            "type": "object",
            "description": "What the next e-mail will use: stored tokens over settings defaults, plus `logo_url`.",
        },
        "stored": {
            "type": "object",
            "description": "Tokens set in the database; a missing key inherits the default.",
        },
        "defaults": {"type": "object", "description": "What an unset token falls back to."},
        "assets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Images on the overrides volume usable as `logo`.",
        },
        "template_overrides": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Templates the overrides volume replaces (relative to `notifications/`).",
        },
        "notification_types": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["effective", "stored", "defaults", "assets", "template_overrides", "notification_types"],
}


def _require_superuser(request, action: str) -> None:
    if not request.user.is_superuser:
        raise ToolError(
            _(
                "%(action)s is restricted to administrators (superusers). Notification settings apply to "
                "every reader of every catalog; `manage` access on a catalog does not confer it."
            )
            % {"action": action}
        )


def _stored(row) -> dict:
    if row is None:
        return {}
    return {name: getattr(row, name) for name in TOKEN_FIELDS if getattr(row, name) not in (None, "")}


def _warnings(effective: branding.Branding) -> list:
    warnings = []
    if effective.logo and not effective.logo_url:
        warnings.append(
            _("Logo asset '%(logo)s' is not on the overrides volume; e-mails fall back to the text header.")
            % {"logo": effective.logo}
        )
    if not branding.overrides_dir():
        warnings.append(_("EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR is not set: no assets or template overrides."))
    return warnings


def _branding_payload() -> dict:
    row = NotificationBranding.current()
    effective = branding.build(row)
    default = branding.defaults()
    return {
        "effective": effective.as_context(),
        "stored": _stored(row),
        "defaults": {name: getattr(default, name) for name in TOKEN_FIELDS},
        "assets": branding.list_assets(),
        "template_overrides": branding.list_template_overrides(),
        "notification_types": TemplateRegistry.get_template_names(),
        "warnings": _warnings(effective),
    }


@registry.tool(
    name="get_notification_branding",
    title="Get notification e-mail branding",
    description=(
        "Show how notification e-mails are branded: the `effective` tokens the next e-mail uses (library "
        "name, logo, colours, footer, From name), which of them are `stored` in the database versus "
        "inherited `defaults`, the image `assets` and `template_overrides` on the deployment's overrides "
        "volume, and every `notification_types` value `preview_notification` accepts. **Restricted to "
        "administrators.** Call this before `update_notification_branding`."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema=BRANDING_OUTPUT_SCHEMA,
    access=ToolAccess.USER,
)
def get_notification_branding(request, raw_arguments: dict) -> dict:
    Arguments(raw_arguments, allowed=())
    _require_superuser(request, _("Notification branding"))
    return _branding_payload()


@registry.tool(
    name="update_notification_branding",
    title="Update notification e-mail branding",
    description=(
        "Change the branding tokens of every notification e-mail this deployment sends. **Restricted to "
        "administrators.** Only the tokens you pass change; list token names in `reset` to return them to "
        "the default. Colours are hex (`#a60c2f`). `logo` names an image from `assets` (see "
        "`get_notification_branding`) or an absolute https URL to a PNG/JPEG — a logo with dark text needs "
        '`header_background: "#ffffff"` and a dark `header_text_color`. Takes effect for the next e-mail; '
        "check the result with `preview_notification` and `send_test_notification` before telling the user "
        "it is done."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **TOKEN_SCHEMA,
            "reset": {
                "type": "array",
                "items": {"type": "string", "enum": list(TOKEN_FIELDS)},
                "description": "Tokens to clear back to their defaults.",
            },
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "changed": {"type": "array", "items": {"type": "string"}},
            **BRANDING_OUTPUT_SCHEMA["properties"],
        },
        "required": ["changed", *BRANDING_OUTPUT_SCHEMA["required"]],
    },
    access=ToolAccess.WRITE,
)
def update_notification_branding(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=(*TOKEN_FIELDS, "reset"))
    _require_superuser(request, _("Notification branding"))

    updates, errors = {}, {}
    for name, limit in TEXT_LIMITS.items():
        value = arguments.string(name, max_length=limit)
        if value is not None:
            updates[name] = value
    # Single-line tokens: a newline in `from_name` would be header injection, in the others it is a mistake.
    for name in ("library_name", "logo", "logo_alt", "logo_link", "website_url", "from_name"):
        if name in updates and any(char in updates[name] for char in "\r\n"):
            errors[name] = _("Must be a single line.")
    width = arguments.integer("logo_width", default=None, minimum=40, maximum=600)
    if width is not None:
        updates["logo_width"] = width
    for name in COLOR_FIELDS:
        value = arguments.string(name, max_length=7)
        if value is None:
            continue
        try:
            updates[name] = branding.validate_color(value)
        except ValueError as error:
            errors[name] = str(error)
    if "logo" in updates:
        try:
            updates["logo"] = branding.validate_logo(updates["logo"])
        except ValueError as error:
            errors["logo"] = str(error)
    for name in ("logo_link", "website_url"):
        if name in updates and not updates[name].startswith(("https://", "http://")):
            errors[name] = _("Must be an absolute http(s) URL.")

    reset = arguments.enum_list("reset", TOKEN_FIELDS)
    reset = reset.split(",") if reset else []
    overlap = sorted(set(reset) & set(updates))
    if overlap:
        errors["reset"] = _("Both set and reset: %(names)s.") % {"names": ", ".join(overlap)}
    if errors:
        raise ToolError("; ".join(f"{name}: {message}" for name, message in sorted(errors.items())))
    if not updates and not reset:
        raise ToolError(_("Pass at least one token to change, or `reset`."))

    row = NotificationBranding.current() or NotificationBranding()
    changed = []
    for name, value in updates.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed.append(name)
    for name in reset:
        empty = None if name == "logo_width" else ""
        if getattr(row, name) not in (None, ""):
            setattr(row, name, empty)
            changed.append(name)
    if changed or row._state.adding:
        row.updated_by = request.user
        row.save()

    logger.info("mcp.write user=%s action=update_notification_branding changed=%s", request.user.pk, ",".join(changed))
    return {"changed": changed, **_branding_payload()}


def _notification_type(arguments: Arguments) -> str:
    types = TemplateRegistry.get_template_names()
    return arguments.enum("notification_type", types, default="license_created")


@registry.tool(
    name="preview_notification",
    title="Preview a notification e-mail",
    description=(
        "Render one notification e-mail with the current branding and sample data — subject, plain text "
        "and the compiled HTML — without sending anything. **Restricted to administrators.** Use it after "
        "`update_notification_branding` to check the result; `send_test_notification` delivers the same "
        "e-mail to your own inbox, which is the only way to see how a mail client renders it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "notification_type": {
                "type": "string",
                "description": "One of `notification_types` from `get_notification_branding`. "
                "Default `license_created`.",
            },
            "include_html": {
                "type": "boolean",
                "default": True,
                "description": "Return the compiled HTML (~20 kB). False returns subject and text only.",
            },
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "notification_type": {"type": "string"},
            "from": {"type": "string"},
            "subject": {"type": "string"},
            "text": {"type": "string"},
            "html": {"type": "string"},
            "logo_url": {"type": "string"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["notification_type", "from", "subject", "text"],
    },
    access=ToolAccess.USER,
)
def preview_notification(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("notification_type", "include_html"))
    _require_superuser(request, _("Notification previews"))
    notification_type = _notification_type(arguments)
    include_html = arguments.boolean("include_html", default=True)

    brand = branding.resolve()
    try:
        rendered = NotificationService.render(
            notification_type, TemplateRegistry.sample_context(request.user.full_name or "Alena"), brand
        )
    except Exception as error:  # a broken override on the volume must be reported, not a 500
        raise ToolError(
            _("Rendering `%(type)s` failed: %(error)s") % {"type": notification_type, "error": error}
        ) from error

    result = {
        "notification_type": notification_type,
        "from": NotificationService.from_address(brand),
        "subject": rendered.subject,
        "text": rendered.text,
        "logo_url": brand.logo_url,
        "warnings": _warnings(brand),
    }
    if include_html:
        result["html"] = rendered.html
    return result


@registry.tool(
    name="send_test_notification",
    title="Send a test notification e-mail to yourself",
    description=(
        "Send one notification e-mail, rendered with the current branding and sample data, to **your own** "
        "primary e-mail contact — never anyone else. **Restricted to administrators.** The send is logged "
        "like any notification. Fails if you have no e-mail contact."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "notification_type": {"type": "string", "description": "Default `license_created`."},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "notification_type": {"type": "string"},
            "status": {"type": "string", "enum": list(NotificationLog.Status.values)},
            "subject": {"type": "string"},
            "log_id": UUID_SCHEMA,
            "error": {"type": ["string", "null"]},
        },
        "required": ["notification_type", "status", "log_id"],
    },
    access=ToolAccess.WRITE,
    idempotent=False,
)
def send_test_notification(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("notification_type",))
    _require_superuser(request, _("Test notifications"))
    notification_type = _notification_type(arguments)

    if not NotificationService.resolve_recipient_email(request.user):
        raise ToolError(_("You have no primary e-mail contact, so there is nowhere to send the test."))

    log = NotificationService.send(
        notification_type=notification_type,
        recipient_user=request.user,
        context={**TemplateRegistry.sample_context(request.user.full_name or "Alena"), "test": True},
    )
    logger.info(
        "mcp.write user=%s action=send_test_notification type=%s status=%s",
        request.user.pk,
        notification_type,
        log.status,
    )
    return {
        "notification_type": notification_type,
        "status": log.status,
        "subject": log.subject or "",
        "log_id": str(log.pk),
        "error": log.error_message or None,
    }


@registry.tool(
    name="sync_ldap_profiles",
    title="Import e-mails of LDAP users from the directory",
    description=(
        "Refresh names and notification e-mail contacts of LDAP users from the directory, without waiting "
        "for them to log in — the backfill for users who have not logged in since the auth source's "
        "`NOTIFICATION_CONTACT_MAP` was configured. **Restricted to administrators.** Run with "
        "`dry_run: true` first. A directory address becomes the primary contact only when the user has none "
        "of their own; addresses users entered themselves are never demoted. Group membership and the "
        "administrator flag are not touched (they still change at login). Results name users and what "
        "changed — never the addresses."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "auth_source_id": {**UUID_SCHEMA, "description": "Default: every active LDAP auth source."},
            "usernames": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only these users (default: every active user of the source).",
            },
            "dry_run": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "dry_run": {"type": "boolean"},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "auth_source": {"type": "string"},
                        "bind": {"type": "string", "enum": ["proxy", "anonymous"]},
                        "summary": {"type": "object"},
                        "error": {"type": "string"},
                        "results": {
                            "type": "array",
                            "description": "Users that changed, were not found, or failed.",
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["auth_source"],
                },
            },
        },
        "required": ["dry_run", "sources"],
    },
    access=ToolAccess.WRITE,
)
def sync_ldap_profiles(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("auth_source_id", "usernames", "dry_run"))
    _require_superuser(request, _("LDAP profile sync"))
    auth_source_id = arguments.uuid("auth_source_id")
    usernames = arguments.string_list("usernames")
    dry_run = arguments.boolean("dry_run", default=False)

    sources = AuthSource.objects.filter(driver=AuthSource.Driver.LDAP, is_active=True)
    if auth_source_id:
        sources = sources.filter(pk=auth_source_id)
        if not sources.exists():
            raise ToolNotFound("LDAP auth source", auth_source_id)
    usernames = assert_within_bulk_limit("usernames", [u.lower() for u in usernames.split(",")]) if usernames else []

    output = []
    for source in sources.order_by("name"):
        users = User.objects.filter(auth_source=source, username__in=usernames) if usernames else None
        try:
            report = sync_users(source, users, dry_run=dry_run)
        except DirectorySyncError as error:
            output.append({"auth_source": source.name, "error": str(error)})
            continue
        output.append(
            {
                "auth_source": source.name,
                "bind": report.bind,
                "summary": report.summary,
                "results": [
                    {
                        "username": r.username,
                        "found": r.found,
                        "error": r.error,
                        "profile": sorted(r.profile),
                        "contacts": [
                            {"type": c.contact_type, "action": c.action, "is_primary": c.is_primary}
                            for c in r.contacts
                        ],
                    }
                    for r in report.results
                    if r.error or not r.found or r.changed
                ],
            }
        )

    if not dry_run:
        logger.info(
            "mcp.write user=%s action=sync_ldap_profiles sources=%s usernames=%s",
            request.user.pk,
            ",".join(s["auth_source"] for s in output),
            len(usernames),
        )
    return {"dry_run": dry_run, "sources": output}
