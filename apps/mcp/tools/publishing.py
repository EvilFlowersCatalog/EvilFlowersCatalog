"""Publishing tools: add a publication to a catalog, correct it, and attach its file.

These answer IP-014 Q12. They reuse REST's own machinery: `EntryForm` validates,
`EntryService.populate` writes (including its duplicate check on title, ISBN and
DOI), and `attach_acquisition` stores the file. An MCP write therefore cannot do
anything, or skip any check, that `POST`/`PUT /api/v1/catalogs/{c}/entries` would not.

What an agent may set is narrower than `EntryForm` on purpose:

- **Descriptive metadata** — title, authors, publisher, date, language, summary,
  identifiers — plus the catalog's own categories and feeds.
- **Readium LCP lending** — `lcp_enabled` and `lcp_copies`, which map to
  `config.readium_enabled` / `config.readium_amount`. Nothing else in `config`
  (OCR, annotations, printing, IP blocking) is reachable from here.
- **No cover images and no inline files.** A file goes through
  `create_upload_link` (see `apps/mcp/uploads.py`), because JSON-RPC cannot
  carry a PDF.

Turning LCP on for an entry that already has a PDF queues encryption right away,
through the same `Entry.post_save` receiver REST relies on. An entry that is
LCP-protected from creation gets its encryption job queued when the file arrives.
"""

import logging
from functools import reduce
from operator import or_

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import QueryDict
from django.utils import timezone
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import EntryForm
from apps.api.services.entry import EntryService
from apps.api.views.entries import _assert_readium_amount_above_active, shelf_record_mapping
from apps.core.models import Acquisition, Entry
from apps.mcp import uploads
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolError, ToolNotFound, ToolPermissionDenied, ToolValidationError
from apps.mcp.projections import entry_detail
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import ENTRY_DETAIL_SCHEMA, UUID_SCHEMA, single_output
from apps.mcp.tools.common import resolve_catalog_for_management
from apps.readium.models import License
from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

logger = logging.getLogger("apps.mcp.audit")

RELATIONS = tuple(Acquisition.AcquisitionType.values)

# Shared by create and update so both describe a field the same way.
ENTRY_FIELDS = {
    "title": {
        "type": "string",
        "maxLength": 255,
        "description": (
            "Publication title. Must be unique within the catalog — the catalog refuses a second "
            "entry with the same title, ISBN or DOI. Distinguish editions in the title, e.g. "
            "'Materiálografia (2018)'."
        ),
    },
    "authors": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Given name(s)."},
                "surname": {"type": "string", "description": "Family name."},
            },
            "required": ["name", "surname"],
            "additionalProperties": False,
        },
        "description": "Authors in credit order. Existing author records in the catalog are reused by exact name.",
    },
    "publisher": {"type": "string", "maxLength": 255},
    "published_at": {
        "type": "string",
        "description": "Publication date; partial dates are fine: 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'.",
    },
    "language_code": {
        "type": "string",
        "description": "ISO 639-1 or 639-2/T code, e.g. 'sk', 'en', 'de'. MARC codes like 'slo' are not accepted.",
    },
    "summary": {"type": "string", "description": "Abstract or blurb shown to readers."},
    "identifiers": {
        "type": "object",
        "properties": {key: {"type": ["string", "null"]} for key in settings.EVILFLOWERS_IDENTIFIERS},
        "additionalProperties": False,
        "description": "Identifiers such as `isbn` and `doi`. On update, keys you pass are merged; null removes one.",
    },
    "category_ids": {
        "type": "array",
        "items": UUID_SCHEMA,
        "description": "Categories from this catalog (`list_categories`). Replaces the current set.",
    },
    "feed_ids": {
        "type": "array",
        "items": UUID_SCHEMA,
        "description": "Acquisition feeds from this catalog (`list_feeds`). Replaces the current set.",
    },
    "lcp_enabled": {
        "type": "boolean",
        "description": (
            "Protect the publication with Readium LCP: readers borrow it instead of downloading it. "
            "Encryption is queued when the file is uploaded (or immediately, if it already has one)."
        ),
    },
    "lcp_copies": {
        "type": "integer",
        "minimum": 1,
        "description": "How many readers may hold an LCP loan at the same time. Required with `lcp_enabled: true`.",
    },
}

EDITABLE = tuple(ENTRY_FIELDS.keys())


def _readable_entry(request, entry_id: str) -> Entry:
    params = QueryDict(mutable=True)
    params["id"] = entry_id
    entry = (
        EntryFilter(params, queryset=Entry.objects.all(), request=request)
        .qs.select_related("catalog", "language")
        .prefetch_related("entry_authors__author", "categories", "acquisitions")
        .first()
    )
    if entry is None:
        raise ToolNotFound("Entry", entry_id)
    return entry


def _manageable_entry(request, entry_id: str, action: str) -> Entry:
    """Read-resolve first (so an unreadable id is 'not found'), then check `check_entry_manage`."""
    entry = _readable_entry(request, entry_id)
    if not has_object_permission("check_entry_manage", request.user, entry):
        raise ToolPermissionDenied(action, f"publication '{entry.title}'")
    return entry


def _payload(arguments: Arguments, raw: dict, *, current_identifiers=None) -> dict:
    """Translate tool arguments into the JSON `EntryForm` expects.

    Only arguments actually sent are included: `django_api_forms` leaves absent
    fields out of `cleaned_data`, so `populate` does not touch them. That is what
    makes `update_entry` a partial update.
    """
    payload = {}
    for key in ("title", "publisher", "published_at", "language_code"):
        value = arguments.string(key, max_length=255)
        if value is not None:
            payload[key] = value
    if "summary" in raw:
        payload["summary"] = arguments.string("summary", max_length=20_000) or ""

    if "authors" in raw:
        authors = raw["authors"]
        if not isinstance(authors, list) or not all(isinstance(item, dict) for item in authors):
            raise ToolError(_("`authors` must be an array of {name, surname} objects."))
        payload["authors"] = [
            {"name": str(item.get("name", "")).strip(), "surname": str(item.get("surname", "")).strip()}
            for item in authors
        ]

    if "identifiers" in raw:
        identifiers = raw["identifiers"]
        if not isinstance(identifiers, dict):
            raise ToolError(_('`identifiers` must be an object, e.g. {"isbn": "978-80-227-5078-3"}.'))
        merged = dict(current_identifiers or {})
        for key, value in identifiers.items():
            if value in (None, ""):
                merged.pop(key, None)
            else:
                merged[key] = str(value).strip()
        payload["identifiers"] = merged

    if "category_ids" in raw:
        payload["category_ids"] = arguments.uuid_sequence("category_ids") or []
    if "feed_ids" in raw:
        payload["feeds"] = arguments.uuid_sequence("feed_ids") or []

    config = {}
    lcp_enabled = arguments.boolean("lcp_enabled")
    if lcp_enabled is not None:
        config["readium_enabled"] = lcp_enabled
    if "lcp_copies" in raw:
        config["readium_amount"] = arguments.integer("lcp_copies", default=None, minimum=1, maximum=100_000)
    if config:
        payload["config"] = config

    return payload


def _validated_form(payload: dict, catalog) -> EntryForm:
    form = EntryForm(payload)
    # Same scoping as REST, plus feeds: nothing from another catalog may be linked.
    form.fields["category_ids"].queryset = form.fields["category_ids"].queryset.filter(catalog=catalog)
    form.fields["author_ids"].queryset = form.fields["author_ids"].queryset.filter(catalog=catalog)
    form.fields["feeds"].queryset = form.fields["feeds"].queryset.filter(catalog=catalog)
    if not form.is_valid():
        raise ToolValidationError(form)
    return form


def _conflict(catalog, title: str, identifiers: dict, *, exclude_pk=None) -> ToolConflict:
    """Name the record that is in the way, so the caller can resume instead of retrying.

    Mirrors the conditions `EntryService.populate` refuses on.
    """
    conditions = [Q(title=title)]
    for key in ("isbn", "doi"):
        if identifiers.get(key):
            conditions.append(Q(**{f"identifiers__{key}": identifiers[key]}))
    clash = Entry.objects.filter(catalog=catalog).exclude(pk=exclude_pk).filter(reduce(or_, conditions)).first()
    if clash is None:
        return ToolConflict(_("A publication with the same title, ISBN or DOI already exists in this catalog."))
    return ToolConflict(
        _(
            "Publication '%(title)s' (%(id)s) already exists in catalog '%(catalog)s' with the same title, "
            "ISBN or DOI. Update that entry instead, or distinguish this one (e.g. add the edition year to the title)."
        )
        % {"title": clash.title, "id": clash.pk, "catalog": catalog.title}
    )


def _entry_payload(request, entry: Entry) -> dict:
    entry = _readable_entry(request, str(entry.pk))
    return {
        "entry": entry_detail(
            entry, request, lcp_state_mapping(request.user, [entry]), shelf_record_mapping(request.user)
        )
    }


def _assert_lcp_coherent(config: dict) -> None:
    if config.get("readium_enabled") and not config.get("readium_amount"):
        raise ToolError(_("`lcp_enabled` needs `lcp_copies` — how many readers may borrow it at once."))


@registry.tool(
    name="create_entry",
    title="Add a publication",
    description=(
        "Create a publication (entry) in a catalog. Requires `manage` access on the catalog. "
        "Pass descriptive metadata and, for a DRM-protected title, `lcp_enabled: true` with "
        "`lcp_copies`. The file is not part of this call — follow up with `create_upload_link` "
        "and POST the PDF/EPUB to the URL it returns.\n\n"
        "The catalog refuses a publication whose title, ISBN or DOI matches one it already holds; "
        "the error names the existing entry so you can update it or skip. Search first with "
        "`search_entries` when importing a list."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Catalog to add the publication to."},
            **ENTRY_FIELDS,
        },
        "required": ["catalog_id", "title", "language_code"],
        "additionalProperties": False,
    },
    output_schema=single_output("entry", ENTRY_DETAIL_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
@transaction.atomic
def create_entry(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("catalog_id", *EDITABLE))
    catalog = resolve_catalog_for_management(
        request, arguments.uuid("catalog_id", required=True), "add publications to"
    )

    payload = _payload(arguments, raw_arguments)
    _assert_lcp_coherent(payload.get("config", {}))
    form = _validated_form(payload, catalog)

    entry = Entry(creator=request.user, catalog=catalog)
    try:
        EntryService(catalog, request.user).populate(entry, form)
    except EntryService.AlreadyExists:
        raise _conflict(catalog, form.cleaned_data["title"], form.cleaned_data.get("identifiers") or {})

    logger.info(
        "mcp.write user=%s action=create_entry entry=%s catalog=%s lcp=%s",
        request.user.pk,
        entry.pk,
        catalog.pk,
        bool(entry.read_config("readium_enabled")),
    )
    return _entry_payload(request, entry)


def _update_lending_only(request, entry: Entry, config: dict) -> dict:
    """`update_entry` with nothing but `lcp_enabled` / `lcp_copies`.

    Skips `EntryForm`: that path rewrites title and language on every call and
    refuses an entry with no language, which several legacy records are. Here
    only the two `config` keys change, saved with `update_fields` so no other
    column is rewritten. `Entry.post_save` still fires, which is what queues
    encryption for any PDF/EPUB not yet encrypted when protection is switched on.
    """
    # Checked on what the caller sent, not the merged config: stored entries carry a
    # default `readium_amount`, and DRM should not switch on with a count nobody stated.
    _assert_lcp_coherent(config)
    merged = {**(entry.config or {}), **config}

    if merged.get("readium_enabled"):
        loans = License.objects.filter(
            entry=entry,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            expires_at__gt=timezone.now(),
        ).count()
        if merged.get("readium_amount") and merged["readium_amount"] < loans:
            raise ToolConflict(
                _("%(loans)d loans of '%(title)s' are out; `lcp_copies` cannot go below that.")
                % {"loans": loans, "title": entry.title}
            )

    if merged != (entry.config or {}):
        entry.config = merged
        entry.save(update_fields=["config", "updated_at", "touched_at"])
        logger.info(
            "mcp.write user=%s action=update_entry entry=%s fields=%s",
            request.user.pk,
            entry.pk,
            ",".join(sorted(config)),
        )
    return _entry_payload(request, entry)


@registry.tool(
    name="update_entry",
    title="Correct a publication",
    description=(
        "Change a publication's metadata or its LCP lending settings. Requires `manage` access. "
        "Only the arguments you pass change; `identifiers` are merged key by key (null removes "
        "one), while `authors`, `category_ids` and `feed_ids` **replace** the whole set.\n\n"
        "Setting `lcp_enabled: true` on a publication that already has a PDF or EPUB queues its "
        "encryption immediately. `lcp_copies` cannot go below the number of loans currently out. "
        "A call that passes only `lcp_enabled` / `lcp_copies` changes those and nothing else — "
        "the way to protect publications that are already in the catalog."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry_id": {**UUID_SCHEMA, "description": "Publication to change, from `search_entries`."},
            **ENTRY_FIELDS,
        },
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("entry", ENTRY_DETAIL_SCHEMA),
    access=ToolAccess.WRITE,
    # Overwrites metadata, and `authors` / `category_ids` / `feed_ids` replace whole sets.
    destructive=True,
)
@transaction.atomic
def update_entry(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_id", *EDITABLE))
    entry = _manageable_entry(request, arguments.uuid("entry_id", required=True), "change")

    payload = _payload(arguments, raw_arguments, current_identifiers=entry.identifiers)
    if len(payload) == 0:
        raise ToolError(_("Nothing to change — pass at least one field besides `entry_id`."))

    if set(payload) == {"config"}:
        return _update_lending_only(request, entry, payload["config"])

    # `EntryForm` requires these two on every write; carry the current values.
    payload.setdefault("title", entry.title)
    if entry.language:
        payload.setdefault("language_code", entry.language.alpha2 or entry.language.alpha3)
    _assert_lcp_coherent(payload.get("config", {}))

    form = _validated_form(payload, entry.catalog)
    _assert_readium_amount_above_active(entry, form)

    try:
        EntryService(entry.catalog, request.user).populate(entry, form)
    except EntryService.AlreadyExists:
        raise _conflict(
            entry.catalog,
            form.cleaned_data["title"],
            form.cleaned_data.get("identifiers") or {},
            exclude_pk=entry.pk,
        )

    logger.info(
        "mcp.write user=%s action=update_entry entry=%s fields=%s",
        request.user.pk,
        entry.pk,
        ",".join(sorted(key for key in raw_arguments if key != "entry_id")),
    )
    return _entry_payload(request, entry)


@registry.tool(
    name="create_upload_link",
    title="Get an upload link for a publication's file",
    description=(
        "Issue a signed, single-use URL for attaching a PDF or EPUB to a publication. Requires "
        "`manage` access. POST the file to `upload_url` as multipart/form-data in a field named "
        "`content` — no Authorization header is needed; the URL is the credential. It expires "
        "after `expires_at` and works once.\n\n"
        "The upload response carries `acquisition_id` and, for an LCP-protected publication, the "
        "`encryption` job (`pending` at first). The publication can be lent once "
        "`list_encryption_jobs` reports it `registered`.\n\n"
        "Refused when the publication already has a file, so a retried import does not attach "
        "the same PDF twice; pass `allow_additional: true` to add a second file on purpose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry_id": {**UUID_SCHEMA, "description": "Publication the file belongs to."},
            "relation": {
                "type": "string",
                "enum": list(RELATIONS),
                "description": "OPDS acquisition relation. Omit for the default 'acquisition'.",
            },
            "allow_additional": {
                "type": "boolean",
                "default": False,
                "description": "Allow a link for a publication that already has a file.",
            },
        },
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "entry_id": UUID_SCHEMA,
            "entry_title": {"type": "string"},
            "lcp_enabled": {"type": "boolean"},
            "upload_url": {"type": "string"},
            "method": {"type": "string"},
            "field": {"type": "string"},
            "accepted_mime_types": {"type": "array", "items": {"type": "string"}},
            "max_bytes": {"type": "integer"},
            "expires_at": {"type": "string", "format": "date-time"},
            "single_use": {"type": "boolean"},
            "example": {"type": "string"},
        },
        "required": ["entry_id", "upload_url", "method", "field", "expires_at"],
    },
    access=ToolAccess.WRITE,
    idempotent=False,
)
def create_upload_link(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_id", "relation", "allow_additional"))
    entry = _manageable_entry(request, arguments.uuid("entry_id", required=True), "upload a file to")
    relation = arguments.enum("relation", RELATIONS)

    if not arguments.boolean("allow_additional", default=False):
        existing = [item for item in entry.acquisitions.all() if item.content]
        if existing:
            raise ToolConflict(
                _(
                    "Publication '%(title)s' already has a file (acquisition %(id)s, %(mime)s). "
                    "Pass `allow_additional: true` to attach another one."
                )
                % {"title": entry.title, "id": existing[0].pk, "mime": existing[0].mime}
            )

    link = uploads.issue(request, entry, request.user, relation=relation)
    logger.info("mcp.write user=%s action=create_upload_link entry=%s", request.user.pk, entry.pk)
    return {
        "entry_id": str(entry.pk),
        "entry_title": entry.title,
        "lcp_enabled": bool(entry.read_config("readium_enabled")),
        **link,
    }
