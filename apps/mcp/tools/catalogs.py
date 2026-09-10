"""Vocabulary tools: the catalogs and authors a search can filter by.

A model that guesses `author="Lovelace"` may or may not hit; one that resolves
the name to an id first always does. These tools exist so a search can be
precise instead of hopeful.

Categories live in `tools/categories.py`, next to their management tools.
"""

from django.http import QueryDict

from apps.api.filters.authors import AuthorFilter
from apps.api.filters.catalogs import CatalogFilter
from apps.core.models import Author, Catalog
from apps.mcp.arguments import Arguments
from apps.mcp.pagination import PAGINATION_ARGUMENTS, paginate, pagination_schema, read_pagination
from apps.mcp.projections import author, catalog
from apps.mcp.registry import registry
from apps.mcp.schemas import AUTHOR_SCHEMA, CATALOG_SCHEMA, UUID_SCHEMA, list_output


def _params(pairs) -> QueryDict:
    params = QueryDict(mutable=True)
    for key, value in pairs:
        if value:
            params[key] = value
    return params


@registry.tool(
    name="list_catalogs",
    title="List catalogs",
    description=(
        "List the catalogs (collections of publications) this credential may read. Use a "
        "catalog id to scope `search_entries` to one collection, or to create feeds and "
        "categories in it. An unauthenticated session sees public catalogs only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Substring match on the catalog title."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(CATALOG_SCHEMA),
)
def list_catalogs(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("title", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)
    params = _params((("title", arguments.string("title")),))

    catalogs = CatalogFilter(params, queryset=Catalog.objects.all(), request=request).qs.order_by("title")
    page = paginate(catalogs, requested_page)

    return {"items": [catalog(item) for item in page.items], "metadata": page.metadata()}


@registry.tool(
    name="list_authors",
    title="List authors",
    description=(
        "Look up authors by name to obtain their ids, then pass those to `search_entries` via "
        "`author_ids` for an exact match. Prefer this over guessing at the free-text `author` "
        "filter when the name spelling is uncertain."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search across first name, surname and full name."},
            "catalog_id": {**UUID_SCHEMA, "description": "Restrict to one catalog."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(AUTHOR_SCHEMA),
)
def list_authors(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("query", "catalog_id", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)
    params = _params(
        (
            ("query", arguments.string("query")),
            ("catalog_id", arguments.uuid("catalog_id")),
        )
    )

    authors = AuthorFilter(params, queryset=Author.objects.all(), request=request).qs.order_by("surname", "name")
    page = paginate(authors, requested_page)

    return {"items": [author(item) for item in page.items], "metadata": page.metadata()}
