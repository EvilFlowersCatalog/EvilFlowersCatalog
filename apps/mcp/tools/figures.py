"""Figure and table search (#159): the MCP face of `GET /api/v1/search`.

The same function answers both (`apps.extraction.figure_search.figure_search`), so the tool inherits the REST
access rules: only publications the credential may see, inside their IP ranges, and of those lent through Readium
LCP only the ones the caller has a live loan of.
"""

from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.extraction.figure_search import ALL_KINDS, DEFAULT_LIMIT, KINDS, MAX_LIMIT, MAX_QUERY_CHARS, figure_search
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError
from apps.mcp.registry import registry
from apps.mcp.schemas import UUID_SCHEMA

FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "entry_id": UUID_SCHEMA,
        "title": {"type": "string", "description": "The publication's title."},
        "page": {"type": "integer", "description": "Page of the original PDF, 1-based."},
        "region_id": UUID_SCHEMA,
        "kind": {"type": "string", "enum": list(KINDS)},
        "caption": {"type": ["string", "null"], "description": "The printed caption, else the description."},
        "description": {"type": ["string", "null"], "description": "A model's description of the figure."},
        "score": {"type": "number"},
        "matched": {"type": "array", "items": {"type": "string", "enum": ["semantic", "words"]}},
        "crop_url": {
            "type": ["string", "null"],
            "description": "The region as an image; needs the same credential to open.",
        },
        "downloadable": {
            "type": "boolean",
            "description": "Whether the crop may be offered for download (open access, printing and sharing allowed).",
        },
        "decision": {
            "type": ["string", "null"],
            "description": "What a decision model labelled the region (chart, diagram, photo, map, table, formula, "
            "...); null when undecided. Uncalibrated.",
        },
        "decision_p": {"type": ["number", "null"], "description": "The model's probability of `decision`."},
    },
    "required": ["entry_id", "title", "page", "region_id", "kind", "score"],
}


@registry.tool(
    name="search_figures",
    title="Search figures and tables",
    description=(
        "Find figures (diagrams, plots, photos) and tables across the library by what they show, e.g. 'PID "
        "controller block diagram'. Returns regions, not publications: each hit has the publication, the page "
        "of the PDF, the caption, a crop URL of the image and a score. Matches by meaning (multilingual, so a "
        "Slovak query finds English figures) and by words. Use `search_entries` for publications and `get_entry` "
        "to read about the one a figure comes from. Publications lent through Readium LCP appear only to a "
        "caller with a live loan of them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What the figure or table shows.", "maxLength": 500},
            "kind": {
                "type": "array",
                "items": {"type": "string", "enum": list(ALL_KINDS)},
                "description": "Region kinds to search (figure, table; both by default), or the labels a decision "
                "model gave figures: chart, diagram, photo, map, formula.",
            },
            "catalog_id": {**UUID_SCHEMA, "description": "Search only this catalog."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kinds": {"type": "array", "items": {"type": "string"}},
            "degraded": {
                "type": "boolean",
                "description": "True when the query could not be embedded: word matches only.",
            },
            "items": {"type": "array", "items": FIGURE_SCHEMA},
        },
        "required": ["query", "kinds", "degraded", "items"],
    },
)
def search_figures(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("query", "kind", "catalog_id", "limit"))
    query = arguments.string("query", max_length=MAX_QUERY_CHARS)
    if not query:
        raise ToolError(_("`query` is required."))
    kinds = arguments.enum_list("kind", ALL_KINDS)
    scope = QueryDict(mutable=True)
    catalog_id = arguments.uuid("catalog_id")
    if catalog_id:
        scope["catalog_id"] = catalog_id
    result = figure_search(
        request,
        query,
        kinds=kinds.split(",") if kinds else KINDS,
        limit=arguments.integer("limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT),
        scope=scope,
    )
    return result.model_dump(mode="json")
