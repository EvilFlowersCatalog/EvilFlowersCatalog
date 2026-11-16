"""The library graph for agents (#161): related sections, prerequisites, concepts, similar books, the map.

Thin wrappers over `apps.graph.queries` and the #152 REST views, with the same rules:

- The caller's credential decides what is visible (`EntryFilter`); an anonymous session sees public catalogs
  only. A section or concept reached only through hidden publications is "not found", never a neighbour.
- A section heading is the publication's text: it is None for titles lent through Readium LCP or outside their
  IP ranges (`apps.graph.views._headings`). The key, the entry and the page stay, so an agent can still cite.
- Without Apache AGE every graph tool answers an empty result with `available: false` and a `note`, not an error.
- Each call records a `graph.query` span, as the REST views do, so tool use shows on the MCP dashboard.
"""

import re
from typing import Dict, Iterable, List, Optional

from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.core.models import Entry
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.registry import registry
from apps.mcp.schemas import UUID_SCHEMA

#: Results per call by default, and at most.
DEFAULT_LIMIT = 5
MAX_LIMIT = 20

_SECTION_KEY = re.compile(r"^[0-9a-f]{20}$")

_WHY = {
    "concepts": "shares concepts",
    "similar": "explains the same thing (similar text)",
    "concepts+similar": "shares concepts and explains the same thing",
}

_NO_GRAPH = "The library graph is not installed on this server; nothing to return."

_LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT}

_SECTION_ARGUMENTS = {
    "entry_id": {**UUID_SCHEMA, "description": "The publication the section belongs to."},
    "section": {
        "type": "string",
        "description": (
            "The section: its `key` as returned by another graph tool (20 hex characters), or its heading "
            "exactly as in the publication's outline (e.g. '2.3 The CFL condition')."
        ),
    },
    "limit": _LIMIT_SCHEMA,
}

_GRAPH_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Section key, for the next graph call."},
        "entry_id": UUID_SCHEMA,
        "title": {"type": "string", "description": "The publication's title."},
        "section": {
            "type": ["string", "null"],
            "description": "The heading; null where the publication's text may not be shown (Readium LCP, IP).",
        },
        "first_page": {"type": ["integer", "null"], "description": "1-based page of the PDF: cite and open here."},
        "score": {"type": "number"},
        "why": {"type": "string"},
    },
    "required": ["key", "entry_id", "score", "why"],
}


def _graph_output(item_schema: dict, **extra) -> dict:
    return {
        "type": "object",
        "properties": {
            "available": {"type": "boolean", "description": "False without the graph: everything is empty."},
            "note": {"type": "string"},
            "items": {"type": "array", "items": item_schema},
            **extra,
        },
        "required": ["available", "items"],
    }


def _span(route: str):
    from apps.graph.views import _span as graph_span

    return graph_span(f"mcp.{route}")


def _unavailable(**extra) -> dict:
    return {"available": False, "note": _NO_GRAPH, "items": [], **extra}


def _limit(arguments: Arguments) -> int:
    return arguments.integer("limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)


def _visible(request) -> List[str]:
    from apps.graph import queries

    return queries.visible_entry_ids(request)


def _section_key(request, entry_id: str, section: str, visible: Iterable[str]) -> str:
    """The graph key of `section` of `entry_id`, after checking the caller may see the publication."""
    from apps.extraction.discovery import _entry_run
    from apps.graph import projection, queries

    if entry_id not in set(visible):
        raise ToolNotFound(_("Publication"), entry_id)
    if _SECTION_KEY.match(section) and queries.section_entry(section) == entry_id:
        return section
    run = _entry_run(entry_id)
    key = projection.section_key(entry_id, section) if run is not None else None
    if key is None or queries.section_entry(key) != entry_id:
        raise ToolError(
            _(
                "Section '%(section)s' of publication %(id)s is not in the library graph. Pass a `key` from "
                "another graph tool or the exact heading; the publication may not be processed yet."
            )
            % {"section": section, "id": entry_id}
        )
    return key


def _items(request, found: List[dict], why) -> List[dict]:
    """Graph sections as tool items: the publication's title, the heading only where it may be shown."""
    from apps.graph.views import _headings

    titles = dict(Entry.objects.filter(pk__in={str(item["entry_id"]) for item in found}).values_list("pk", "title"))
    titles = {str(pk): title for pk, title in titles.items()}
    return [
        {
            "key": item["key"],
            "entry_id": str(item["entry_id"]),
            "title": titles.get(str(item["entry_id"]), ""),
            "section": item["title"],
            "first_page": item["first_page"],
            "score": float(item["score"]),
            "why": why(item),
        }
        for item in _headings(request, found)
    ]


#: #190: the decider's relation, as the reader of the asked section sees the other one.
_RELATION_WHY = {
    "same": "explains the same idea",
    "prerequisite": "read it first: a prerequisite",
    "next": "builds on this section",
    "example": "an example of this section's idea",
    "general": "the general idea this section is an example of",
    "loose": "loosely related",
}


def _related_why(item: dict) -> str:
    why = _WHY.get(item["via"], item["via"])
    relation = _RELATION_WHY.get(item.get("relation") or "")
    return f"{why}; {relation}" if relation else why


@registry.tool(
    name="related_sections",
    title="Find related sections in other books",
    description=(
        "Sections of OTHER publications that explain the same thing as one section: they share its concepts or "
        "their text is similar across languages (an English section can be related to a Slovak or German one). "
        "Best first; each carries the page to cite. Use it to follow 'explained elsewhere' across the library."
    ),
    input_schema={
        "type": "object",
        "properties": _SECTION_ARGUMENTS,
        "required": ["entry_id", "section"],
        "additionalProperties": False,
    },
    output_schema=_graph_output(_GRAPH_SECTION_SCHEMA),
)
def related_sections(request, raw_arguments: dict) -> dict:
    from apps.graph import age, queries

    arguments = Arguments(raw_arguments, allowed=("entry_id", "section", "limit"))
    entry_id = arguments.uuid("entry_id", required=True)
    section = arguments.string("section", max_length=500) or ""
    limit = _limit(arguments)
    with _span("section.related") as current:
        if not age.available():
            return _unavailable()
        visible = _visible(request)
        key = _section_key(request, entry_id, section, visible)
        found = _items(request, queries.related_sections(key, visible, limit), _related_why)
        current.set_attribute("evilflowers.graph.rows", len(found))
    return {"available": True, "items": found}


@registry.tool(
    name="section_prerequisites",
    title="List what to read before a section",
    description=(
        "Sections that define a concept this section only uses: what to read first. Earlier sections of the "
        "same book and sections of other books come first; a section further on in the same book comes last "
        "with `later: true` ('defined later in this book'). Each carries the page to cite."
    ),
    input_schema={
        "type": "object",
        "properties": _SECTION_ARGUMENTS,
        "required": ["entry_id", "section"],
        "additionalProperties": False,
    },
    output_schema=_graph_output(
        {
            **_GRAPH_SECTION_SCHEMA,
            "properties": {**_GRAPH_SECTION_SCHEMA["properties"], "later": {"type": "boolean"}},
        }
    ),
)
def section_prerequisites(request, raw_arguments: dict) -> dict:
    from apps.graph import age, queries

    arguments = Arguments(raw_arguments, allowed=("entry_id", "section", "limit"))
    entry_id = arguments.uuid("entry_id", required=True)
    section = arguments.string("section", max_length=500) or ""
    limit = _limit(arguments)
    with _span("section.prerequisites") as current:
        if not age.available():
            return _unavailable()
        visible = _visible(request)
        key = _section_key(request, entry_id, section, visible)
        found = queries.prerequisites(key, visible, limit)
        later = {item["key"]: item["later"] for item in found}
        items = _items(
            request,
            found,
            lambda item: (
                "defines a concept this section uses, later in the same book"
                if item["later"]
                else "defines a concept this section uses"
            ),
        )
        for item in items:
            item["later"] = later[item["key"]]
        current.set_attribute("evilflowers.graph.rows", len(items))
    return {"available": True, "items": items}


@registry.tool(
    name="find_concept",
    title="Find concepts",
    description=(
        "Concepts (terms) the caller's publications define or discuss, most used first. `q` matches the term, "
        "accents and case ignored; an empty `q` lists the most used. The same term in two fields (UDC main "
        "classes) is two concepts, each with its `field`. Follow up with `get_concept` on a `key`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Part of the term, e.g. 'CFL' or 'stabilita'."},
            "limit": {**_LIMIT_SCHEMA, "default": 10},
        },
        "additionalProperties": False,
    },
    output_schema=_graph_output(
        {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "term": {"type": "string"},
                "field": {"type": ["string", "null"]},
                "publications": {"type": "integer"},
                "sections": {"type": "integer"},
            },
            "required": ["key", "term"],
        }
    ),
)
def find_concept(request, raw_arguments: dict) -> dict:
    from apps.graph import age, queries

    arguments = Arguments(raw_arguments, allowed=("q", "limit"))
    query = arguments.string("q", default="", max_length=120)
    limit = arguments.integer("limit", default=10, minimum=1, maximum=MAX_LIMIT)
    with _span("concept.list") as current:
        if not age.available():
            return _unavailable()
        found = queries.search_concepts(query, _visible(request), limit)
        current.set_attribute("evilflowers.graph.rows", len(found))
    return {"available": True, "items": found}


@registry.tool(
    name="get_concept",
    title="Get a concept",
    description=(
        "A concept with the caller's sections that define or discuss it (defining sections first), each with "
        "its publication and page, plus the subjects those publications are filed under."
    ),
    input_schema={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "A concept `key` from `find_concept`."}},
        "required": ["key"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "available": {"type": "boolean"},
            "note": {"type": "string"},
            "concept": {
                "type": ["object", "null"],
                "properties": {
                    "key": {"type": "string"},
                    "term": {"type": "string"},
                    "field": {"type": ["string", "null"]},
                    "sections": {"type": "array", "items": _GRAPH_SECTION_SCHEMA},
                    "subjects": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"id": UUID_SCHEMA, "term": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "required": ["available", "concept"],
    },
)
def get_concept(request, raw_arguments: dict) -> dict:
    from apps.core.models import Category
    from apps.graph import age, queries

    arguments = Arguments(raw_arguments, allowed=("key",))
    key = arguments.string("key", max_length=300) or ""
    with _span("concept.detail") as current:
        if not age.available():
            return {"available": False, "note": _NO_GRAPH, "concept": None}
        found = queries.concept(key, _visible(request))
        if found is None:
            raise ToolNotFound(_("Concept"), key)
        sections = _items(
            request, [{**section, "score": 1.0} for section in found["sections"]], lambda item: item["role"]
        )
        current.set_attribute("evilflowers.graph.rows", len(sections))
    entry_ids = list(dict.fromkeys(item["entry_id"] for item in sections))
    subjects = Category.objects.filter(entries__in=entry_ids).distinct().order_by("term").values("id", "term")
    return {
        "available": True,
        "concept": {
            "key": found["key"],
            "term": found["term"],
            "field": found["field"],
            "sections": sections,
            "subjects": [{"id": str(subject["id"]), "term": subject["term"]} for subject in subjects],
        },
    }


def _scope(catalog_id: Optional[str]) -> QueryDict:
    scope = QueryDict(mutable=True)
    if catalog_id:
        scope["catalog_id"] = catalog_id
    return scope


@registry.tool(
    name="similar_entries",
    title="Find similar publications",
    description=(
        "Publications most like one publication, by the meaning of their whole text (mutual nearest "
        "neighbours, centred per language): the same edges as the library map. Only processed publications "
        "take part; an empty list means none is close enough, or the publication is not processed yet."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry_id": UUID_SCHEMA,
            "catalog_id": {**UUID_SCHEMA, "description": "Only neighbours within this catalog."},
        },
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"entry_id": UUID_SCHEMA, "title": {"type": "string"}, "score": {"type": "number"}},
                    "required": ["entry_id", "score"],
                },
            }
        },
        "required": ["items"],
    },
)
def similar_entries(request, raw_arguments: dict) -> dict:
    from apps.graph.views import entry_neighbours

    arguments = Arguments(raw_arguments, allowed=("entry_id", "catalog_id"))
    entry_id = arguments.uuid("entry_id", required=True)
    catalog_id = arguments.uuid("catalog_id")
    with _span("entries.similar") as current:
        if entry_id not in set(_visible(request)):
            raise ToolNotFound(_("Publication"), entry_id)
        neighbours = entry_neighbours(request, _scope(catalog_id)).get(entry_id, [])
        current.set_attribute("evilflowers.graph.rows", len(neighbours))
    titles: Dict[str, str] = {
        str(pk): title
        for pk, title in Entry.objects.filter(pk__in=[other for other, _s in neighbours]).values_list("pk", "title")
    }
    return {
        "items": [{"entry_id": other, "title": titles.get(other, ""), "score": score} for other, score in neighbours]
    }


@registry.tool(
    name="library_map",
    title="Get the library map",
    description=(
        "The map of the caller's processed publications: nodes (title, UDC category terms, pages) and edges "
        "between publications that are among each other's nearest by meaning. For drawing or clustering the "
        "library; for one publication's neighbours use `similar_entries`."
    ),
    input_schema={
        "type": "object",
        "properties": {"catalog_id": {**UUID_SCHEMA, "description": "Only this catalog."}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"type": "object"}},
            "edges": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["nodes", "edges"],
    },
)
def library_map(request, raw_arguments: dict) -> dict:
    from apps.graph.views import library_map as build_map

    arguments = Arguments(raw_arguments, allowed=("catalog_id",))
    catalog_id = arguments.uuid("catalog_id")
    with _span("entries.map") as current:
        _etag, payload = build_map(request, _scope(catalog_id))
        current.set_attribute("evilflowers.graph.rows", len(payload["edges"]))
    return payload


@registry.tool(
    name="cited_by",
    title="Find what cites a publication",
    description=(
        "Publications of the library whose reference lists cite this one (#178), matched by DOI or ISBN, each "
        "with the reference item's address (`uri`, `url`) and page. Also `cites`: what this publication's own "
        "reference list cites in the library. Only publications you may see are listed."
    ),
    input_schema={
        "type": "object",
        "properties": {"entry_id": UUID_SCHEMA},
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "cited_by": {"type": "array", "items": {"type": "object"}},
            "cites": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["entry_id", "cited_by", "cites"],
    },
)
def cited_by(request, raw_arguments: dict) -> dict:
    from apps.extraction.bibliography import citations_of

    arguments = Arguments(raw_arguments, allowed=("entry_id",))
    entry_id = arguments.uuid("entry_id", required=True)
    if entry_id not in set(_visible(request)):
        raise ToolNotFound("Entry", entry_id)
    with _span("entries.cited_by"):
        return citations_of(request, entry_id)
