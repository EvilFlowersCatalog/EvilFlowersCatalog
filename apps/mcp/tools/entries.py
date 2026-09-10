"""Discovery tools: find publications, read one in full, and file them by subject.

Every tool here resolves its queryset through `apps.api.filters.entries.EntryFilter`
rather than querying `Entry` directly. That filter is where catalog access
control lives (`BaseSecuredFilter.apply_catalog_access_control`), so routing
through it means MCP cannot drift from REST on who may see what — there is one
implementation, not two.

`classify_entries` is the one write here. It touches only the `categories`
relation: an agent may decide that a book belongs under "624 Stavebné
inžinierstvo", but it may not rewrite the book's title, authors, files or
identifiers. That line is deliberate — classification is reversible and
inspectable in a way that metadata rewriting is not.
"""

import logging

from django.db import transaction
from django.http import QueryDict
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.categories import CategoryFilter
from apps.api.filters.entries import EntryFilter
from apps.api.views.entries import shelf_record_mapping
from apps.core.models import Category, Entry
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound, ToolPermissionDenied
from apps.mcp.pagination import PAGINATION_ARGUMENTS, pagination_schema, paginate, read_pagination
from apps.mcp.projections import entry_detail, entry_summary
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import ENTRY_DETAIL_SCHEMA, ENTRY_SUMMARY_SCHEMA, UUID_SCHEMA, list_output, single_output
from apps.mcp.tools.common import assert_same_catalog, assert_within_bulk_limit, resolve_all
from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

logger = logging.getLogger("apps.mcp.audit")

LCP_STATES = (
    "not_lcp",
    "available_now",
    "available_in_days",
    "active_loan_for_user",
    "fully_borrowed",
)

ORDER_BY = (
    "-created_at",
    "created_at",
    "-popularity",
    "popularity",
    "title",
    "-title",
    "-published_at",
    "published_at",
)

SEARCH_ARGUMENTS = (
    "query",
    "title",
    "author",
    "author_ids",
    "catalog_id",
    "catalog_title",
    "category_ids",
    "category_term",
    "language_codes",
    "published_from",
    "published_to",
    "lcp_states",
    "readium_only",
    "order_by",
    *PAGINATION_ARGUMENTS,
)

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Free-text search across title, summary, publisher, author names and category "
                "labels. All whitespace-separated terms must match somewhere in the record. "
                "Results come back ranked by relevance unless `order_by` overrides it."
            ),
        },
        "title": {"type": "string", "description": "Case- and accent-insensitive substring match on the title alone."},
        "author": {
            "type": "string",
            "description": "Author name substring — matched against first name, surname and the combined full name.",
        },
        "author_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Restrict to publications by any of these authors (from `list_authors`).",
        },
        "catalog_id": {"type": "string", "format": "uuid", "description": "Restrict to one catalog."},
        "catalog_title": {"type": "string", "description": "Restrict to catalogs whose title contains this text."},
        "category_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Restrict to publications in any of these categories (from `list_categories`).",
        },
        "category_term": {"type": "string", "description": "Restrict to an exact category term, e.g. 'mathematics'."},
        "language_codes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ISO 639 language codes, 2- or 3-letter, e.g. ['sk', 'en'].",
        },
        "published_from": {
            "type": "string",
            "description": "Earliest publication date. Partial dates are accepted: 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'.",
        },
        "published_to": {
            "type": "string",
            "description": "Latest publication date, same partial-date formats as `published_from`.",
        },
        "lcp_states": {
            "type": "array",
            "items": {"type": "string", "enum": list(LCP_STATES)},
            "description": (
                "Filter by borrowing availability. 'available_now' = a DRM licence can be issued "
                "immediately; 'fully_borrowed' = every copy is out; 'active_loan_for_user' = the "
                "calling user already has it on loan; 'not_lcp' = freely downloadable, no licence needed."
            ),
        },
        "readium_only": {
            "type": "boolean",
            "description": "True returns only Readium LCP (DRM-protected, borrowable) publications.",
        },
        "order_by": {
            "type": "string",
            "enum": list(ORDER_BY),
            "description": (
                "Explicit sort. Omit it with `query` set to keep relevance ranking; omit it "
                "without `query` to get newest first."
            ),
        },
        **pagination_schema(),
    },
    "additionalProperties": False,
}


def _search_queryset(request, arguments: Arguments):
    """Translate tool arguments into the GET parameters `EntryFilter` understands."""
    params = QueryDict(mutable=True)

    for key, value in (
        ("query", arguments.string("query")),
        ("title", arguments.string("title")),
        ("author", arguments.string("author")),
        ("author_id", arguments.uuid_list("author_ids")),
        ("catalog_id", arguments.uuid("catalog_id")),
        ("catalog_title", arguments.string("catalog_title")),
        ("category_id", arguments.uuid_list("category_ids")),
        ("category_term", arguments.string("category_term")),
        ("language_code", arguments.string_list("language_codes")),
        ("published_at__gte", arguments.string("published_from")),
        ("published_at__lte", arguments.string("published_to")),
        ("lcp_state", arguments.enum_list("lcp_states", LCP_STATES)),
    ):
        if value:
            params[key] = value

    readium_only = arguments.boolean("readium_only")
    if readium_only is not None:
        params["config__readium_enabled"] = "true" if readium_only else "false"

    return (
        EntryFilter(params, queryset=Entry.objects.all(), request=request)
        .qs.select_related("language")
        .prefetch_related("entry_authors__author", "categories")
        .distinct()
    )


@registry.tool(
    name="search_entries",
    title="Search publications",
    description=(
        "Search the publication catalog by free text and/or structured filters (author, "
        "category, language, publication date, catalog, borrowing availability). Returns a "
        "page of compact records — title, authors, categories, a truncated summary and, for "
        "DRM-protected titles, live borrowing availability. Results are always limited to the "
        "catalogs the calling credential may read; an unauthenticated session sees public "
        "catalogs only. Call `get_entry` for the full record of a specific result."
    ),
    input_schema=SEARCH_SCHEMA,
    output_schema=list_output(ENTRY_SUMMARY_SCHEMA),
)
def search_entries(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=SEARCH_ARGUMENTS)
    requested_page = read_pagination(arguments)
    entries = _search_queryset(request, arguments)

    order_by = arguments.enum("order_by", ORDER_BY)
    if order_by:
        entries = entries.order_by(order_by)
    elif not arguments.string("query"):
        entries = entries.order_by("-created_at")
    # With `query` and no explicit `order_by` we leave the relevance ordering
    # `EntryFilter.filter_query` installed. Re-sorting here would discard it —
    # the same mistake `PaginationResponse` makes on the REST list endpoint,
    # where a default `created_at` ordering clobbers relevance.

    page = paginate(entries, requested_page)
    lcp_states = lcp_state_mapping(request.user, page.items)
    shelf = shelf_record_mapping(request.user) if request.user.is_authenticated else {}

    return {
        "items": [entry_summary(entry, request, lcp_states, shelf) for entry in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="get_entry",
    title="Get publication details",
    description=(
        "Fetch one publication in full: complete summary, description, identifiers (ISBN, DOI, "
        "…), table of contents, citation, cover image, downloadable files and current borrowing "
        "availability. Takes an entry id from `search_entries`. Long prose is truncated — the "
        "response flags it when that happens."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "format": "uuid",
                "description": "Entry id, as returned by `search_entries`.",
            }
        },
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("entry", ENTRY_DETAIL_SCHEMA),
)
def get_entry(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_id",))
    entry_id = arguments.uuid("entry_id", required=True)

    # Resolved through `EntryFilter` for the same reason the search is: it
    # applies catalog access control. Fetching `Entry.objects.get(...)` and
    # then calling the `check_entry_read` checker would be *stricter* than the
    # search that produced the id (that checker rejects anonymous readers even
    # for public catalogs), so an agent could find an entry it could not then
    # open. See IP-014 Q1.
    params = QueryDict(mutable=True)
    params["id"] = entry_id

    entry = (
        EntryFilter(params, queryset=Entry.objects.all(), request=request)
        .qs.select_related("language", "catalog")
        .prefetch_related("entry_authors__author", "categories", "acquisitions")
        .first()
    )

    if entry is None:
        raise ToolNotFound("Entry", entry_id)

    lcp_states = lcp_state_mapping(request.user, [entry])
    shelf = shelf_record_mapping(request.user) if request.user.is_authenticated else {}

    return {"entry": entry_detail(entry, request, lcp_states, shelf)}


CLASSIFY_MODES = ("add", "replace", "remove")


def _manageable_entries(request, entries: list) -> list:
    """Filter `entries` to the ones this credential may re-classify.

    Mirrors `check_entry_manage`: the catalog's managers, plus an entry's own
    creator. Evaluated in bulk rather than one `has_object_permission` call per
    entry, which on a hundred-entry batch would be a hundred queries — the
    catalog check is asked once, and creator ownership is a field comparison.
    """
    catalog = entries[0].catalog
    if has_object_permission("check_catalog_manage", request.user, catalog):
        return entries

    return [entry for entry in entries if entry.creator_id == request.user.pk]


@registry.tool(
    name="classify_entries",
    title="File publications under categories",
    description=(
        "Add, replace or remove subject categories on a batch of publications — the tool for "
        "classifying a collection against a scheme such as UDC/MDT. Requires `manage` access on "
        "the catalog (or being the publication's own creator). Every entry and every category "
        "must belong to the same catalog; create the vocabulary first with `create_categories`. "
        "\n\n"
        "`mode` decides what happens to classifications already on a record: 'add' (the default) "
        "keeps them and adds yours, 'remove' takes only the named ones away, and 'replace' "
        "discards everything else the record was filed under. Prefer 'add' unless the user asked "
        "for a re-classification. The result reports per publication whether anything actually "
        "changed, so re-running the same call is safe and reports zero changes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry_ids": {
                "type": "array",
                "items": UUID_SCHEMA,
                "description": "Publications to file, from `search_entries`. All must share one catalog.",
            },
            "category_ids": {
                "type": "array",
                "items": UUID_SCHEMA,
                "description": (
                    "Categories to apply, from `list_categories` or `create_categories`. May be "
                    "empty only with mode 'replace', which then clears every classification."
                ),
            },
            "mode": {
                "type": "string",
                "enum": list(CLASSIFY_MODES),
                "default": "add",
                "description": "'add' keeps existing categories, 'remove' detaches, 'replace' overwrites the set.",
            },
        },
        "required": ["entry_ids", "category_ids"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "changed": {"type": "integer"},
            "unchanged": {"type": "integer"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": UUID_SCHEMA,
                        "title": {"type": "string"},
                        "changed": {"type": "boolean"},
                        "categories": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "changed", "categories"],
                },
            },
        },
        "required": ["mode", "changed", "unchanged", "results"],
    },
    access=ToolAccess.WRITE,
    # Re-running the same call is a no-op, and the result says so.
    idempotent=True,
    # 'replace' and 'remove' discard classifications the caller did not name.
    destructive=True,
)
@transaction.atomic
def classify_entries(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_ids", "category_ids", "mode"))

    mode = arguments.enum("mode", CLASSIFY_MODES, default="add")
    entry_ids = assert_within_bulk_limit("entry_ids", arguments.uuid_sequence("entry_ids") or [])
    category_ids = arguments.uuid_sequence("category_ids") or []

    if not category_ids and mode != "replace":
        raise ToolError(
            _("`category_ids` is empty. Only mode 'replace' accepts that, and it clears every classification.")
        )

    entries = resolve_all(
        request,
        EntryFilter,
        Entry,
        entry_ids,
        label="Entry",
        queryset=Entry.objects.select_related("catalog").prefetch_related("categories"),
    )
    catalog = entries[0].catalog
    assert_same_catalog(catalog, entries, label=_("Entries"))

    categories = resolve_all(request, CategoryFilter, Category, category_ids, label="Category")
    assert_same_catalog(catalog, categories, label=_("Categories"))

    # Refuse the whole batch rather than classify the permitted half: a partial
    # write the caller has to reconcile is worse than a refusal it can act on.
    permitted = _manageable_entries(request, entries)
    if len(permitted) != len(entries):
        raise ToolPermissionDenied(
            "classify",
            _("%(count)d of the %(total)d publications requested")
            % {"count": len(entries) - len(permitted), "total": len(entries)},
        )

    results = []
    for entry in entries:
        before = {category.pk for category in entry.categories.all()}

        if mode == "add":
            entry.categories.add(*categories)
        elif mode == "remove":
            entry.categories.remove(*categories)
        else:
            entry.categories.set(categories)

        after = set(entry.categories.values_list("pk", flat=True))
        results.append(
            {
                "id": str(entry.pk),
                "title": entry.title,
                "changed": before != after,
                "categories": sorted(entry.categories.values_list("term", flat=True)),
            }
        )

    changed = sum(1 for result in results if result["changed"])
    logger.info(
        "mcp.write user=%s action=classify_entries mode=%s catalog=%s entries=%s categories=%s changed=%s",
        request.user.pk,
        mode,
        catalog.pk,
        len(entries),
        len(categories),
        changed,
    )

    return {
        "mode": mode,
        "changed": changed,
        "unchanged": len(results) - changed,
        "results": results,
    }
