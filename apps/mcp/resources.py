"""MCP resources: the catalog as addressable, readable context.

Tools are verbs a model chooses to call. Resources are nouns a *client* can
browse, attach to a conversation, or let a human pick from a list — the same
data, reached without the model deciding to go looking for it.

What is exposed:

- `resources/list` enumerates catalogs (few, stable, worth browsing) with
  cursor pagination.
- `resources/templates/list` advertises `evilflowers://entry/{id}` and
  `evilflowers://feed/{id}`. Entries are **not** enumerated: ~1000 of them
  would flood a picker, and `search_entries` is the right way to find one.
- `resources/read` resolves any `evilflowers://` URI through the same filters
  the tools use, so a resource can never reveal what a tool would refuse.

Every resource read is access-controlled exactly like its tool equivalent.
"""

import json
from typing import Optional

from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.api.filters.catalogs import CatalogFilter
from apps.api.filters.entries import EntryFilter
from apps.api.filters.feeds import FeedFilter
from apps.core.models import Catalog, Entry, Feed
from apps.mcp import uris
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.projections import catalog as project_catalog
from apps.mcp.projections import entry_detail
from apps.mcp.projections import feed as project_feed
from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

MIME = "application/json"

#: Cap on one `resources/list` page. Clients that ignore `nextCursor` still get
#: a bounded response.
PAGE_SIZE = 100


def _descriptor(uri: str, name: str, description: str, **extra) -> dict:
    return {"uri": uri, "name": name, "title": name, "description": description, "mimeType": MIME, **extra}


def list_resources(request, cursor: Optional[str]) -> dict:
    """Catalogs, as browsable resources, one page at a time.

    The cursor is an opaque offset. Opaque is the contract: a client must round
    trip whatever it was handed and must not construct one, which leaves us free
    to change the encoding later.
    """
    offset = _decode_cursor(cursor)

    catalogs = CatalogFilter(QueryDict(mutable=True), queryset=Catalog.objects.all(), request=request).qs.order_by(
        "title", "id"
    )[offset : offset + PAGE_SIZE + 1]
    catalogs = list(catalogs)

    has_more = len(catalogs) > PAGE_SIZE
    catalogs = catalogs[:PAGE_SIZE]

    payload = {
        "resources": [
            _descriptor(
                uris.catalog_uri(item.pk),
                item.title,
                _("Catalog '%(title)s' — its feeds, subjects and publication count.") % {"title": item.title},
            )
            for item in catalogs
        ]
    }
    if has_more:
        payload["nextCursor"] = _encode_cursor(offset + PAGE_SIZE)
    return payload


def list_resource_templates(request) -> dict:
    """URI templates for the collections too large to enumerate."""
    return {
        "resourceTemplates": [
            {
                "uriTemplate": "evilflowers://entry/{id}",
                "name": "Publication",
                "title": "Publication",
                "description": (
                    "One publication in full, by id. Find ids with the `search_entries` tool — "
                    "the catalog holds too many publications to list here."
                ),
                "mimeType": MIME,
            },
            {
                "uriTemplate": "evilflowers://feed/{id}",
                "name": "Feed",
                "title": "Feed",
                "description": "One feed (a curated collection or navigation node), by id. See the `list_feeds` tool.",
                "mimeType": MIME,
            },
            {
                "uriTemplate": "evilflowers://catalog/{id}",
                "name": "Catalog",
                "title": "Catalog",
                "description": "One catalog, by id. Catalogs are also enumerated directly under `resources/list`.",
                "mimeType": MIME,
            },
        ]
    }


def read_resource(request, uri: str) -> dict:
    """Resolve a URI to its content, or explain why it cannot be resolved."""
    reference = uris.parse(uri)
    if reference is None:
        raise ToolError(
            _("'%(uri)s' is not a resource URI this server serves. Expected one of: %(shapes)s.")
            % {
                "uri": uri,
                "shapes": ", ".join(f"evilflowers://{kind}/<uuid>" for kind in uris.KINDS),
            }
        )

    readers = {uris.CATALOG: _read_catalog, uris.ENTRY: _read_entry, uris.FEED: _read_feed}
    payload = readers[reference.kind](request, reference.identifier)

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": MIME,
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ]
    }


def _read_catalog(request, identifier: str) -> dict:
    catalog = CatalogFilter(
        QueryDict(mutable=True), queryset=Catalog.objects.filter(pk=identifier), request=request
    ).qs.first()
    if catalog is None:
        raise ToolNotFound("Catalog", identifier)

    payload = project_catalog(catalog)
    # A catalog on its own is four fields; the counts are what make it worth
    # attaching to a conversation.
    payload["entry_count"] = (
        EntryFilter(QueryDict(mutable=True), queryset=Entry.objects.filter(catalog=catalog), request=request)
        .qs.distinct()
        .count()
    )
    payload["feed_count"] = (
        FeedFilter(QueryDict(mutable=True), queryset=Feed.objects.filter(catalog=catalog), request=request)
        .qs.distinct()
        .count()
    )
    return payload


def _read_entry(request, identifier: str) -> dict:
    params = QueryDict(mutable=True)
    params["id"] = identifier

    entry = (
        EntryFilter(params, queryset=Entry.objects.all(), request=request)
        .qs.select_related("language", "catalog")
        .prefetch_related("entry_authors__author", "categories", "acquisitions")
        .first()
    )
    if entry is None:
        raise ToolNotFound("Entry", identifier)

    return entry_detail(entry, request, lcp_state_mapping(request.user, [entry]))


def _read_feed(request, identifier: str) -> dict:
    feed = (
        FeedFilter(QueryDict(mutable=True), queryset=Feed.objects.filter(pk=identifier), request=request)
        .qs.select_related("catalog")
        .first()
    )
    if feed is None:
        raise ToolNotFound("Feed", identifier)

    return project_feed(feed, request)


def _encode_cursor(offset: int) -> str:
    return str(offset)


def _decode_cursor(cursor: Optional[str]) -> int:
    if cursor in (None, ""):
        return 0
    try:
        offset = int(cursor)
    except (TypeError, ValueError):
        raise ToolError(_("Invalid cursor. Pass back the `nextCursor` value from the previous page verbatim."))
    if offset < 0:
        raise ToolError(_("Invalid cursor."))
    return offset


__all__ = ["list_resource_templates", "list_resources", "read_resource"]
