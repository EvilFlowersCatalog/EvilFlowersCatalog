"""Page slicing shared by every list-shaped tool.

`apps.api.response.PaginationResponse` cannot be reused here: it paginates *and*
writes an `HttpResponse`, while a tool needs the materialised page so it can
build a per-page LCP mapping before projecting. This keeps the same
`page`/`limit`/`pages`/`total` vocabulary the REST API uses so the two surfaces
describe results identically.
"""

from dataclasses import dataclass
from typing import List

from django.conf import settings
from django.core.paginator import Paginator

from apps.mcp.arguments import Arguments

PAGINATION_ARGUMENTS = ("page", "limit")


def pagination_schema() -> dict:
    return {
        "page": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "1-based page number. Use `metadata.pages` from a previous call to walk the result set.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": settings.EVILFLOWERS_MCP_MAX_LIMIT,
            "default": settings.EVILFLOWERS_MCP_DEFAULT_LIMIT,
            "description": (
                "Results per page. Keep it small — every result consumes context. "
                f"Maximum {settings.EVILFLOWERS_MCP_MAX_LIMIT}."
            ),
        },
    }


@dataclass(frozen=True)
class PageRequest:
    """A validated `page`/`limit` pair.

    Read at the *top* of a handler, before any queryset is built. Access
    control resolves eagerly (`get_accessible_catalog_ids` runs a query while
    the filter's `qs` is assembled), so validating pagination afterwards would
    mean a call with `limit=5000` still costs a round-trip before being told no.
    """

    page: int
    limit: int


def read_pagination(arguments: Arguments) -> PageRequest:
    return PageRequest(
        limit=arguments.integer(
            "limit",
            default=settings.EVILFLOWERS_MCP_DEFAULT_LIMIT,
            minimum=1,
            maximum=settings.EVILFLOWERS_MCP_MAX_LIMIT,
        ),
        page=arguments.integer("page", default=1, minimum=1, maximum=100_000),
    )


@dataclass
class Page:
    items: List
    page: int
    limit: int
    pages: int
    total: int

    def metadata(self) -> dict:
        return {
            "page": self.page,
            "limit": self.limit,
            "pages": self.pages,
            "total": self.total,
            "has_next_page": self.page < self.pages,
        }


def paginate(queryset, requested: PageRequest) -> Page:
    limit = requested.limit
    page_number = requested.page

    paginator = Paginator(queryset, limit)
    # An out-of-range page is clamped to the last page rather than raising: a
    # model walking pages should be told "nothing more here", not handed a
    # failure to recover from. This deliberately differs from the REST API,
    # which 404s on `EmptyPage`.
    effective_page = min(page_number, paginator.num_pages)
    page = paginator.get_page(effective_page)

    return Page(
        items=list(page.object_list),
        page=effective_page,
        limit=limit,
        pages=paginator.num_pages,
        total=paginator.count,
    )


__all__ = ["PAGINATION_ARGUMENTS", "Page", "PageRequest", "paginate", "pagination_schema", "read_pagination"]
