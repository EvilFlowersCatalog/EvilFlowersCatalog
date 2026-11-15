"""Reading publications (#176): graded, citable reads that obey the same content gate as every other surface.

Every call resolves the publication through the catalog ACL (hidden is not found) and the content gate
(`apps.content.access`): the caller's tier (`metadata < outline < snippet < passage < full`) decides what comes
back, and each result carries the receipt (`access`: tier, basis, policy, how to unlock). Text is addressed by
locators — `evilflowers://entry/<id>/locator/<key>`, also as a `/cite/<id>/<key>` link a person can open — so an
agent keeps addresses, not copies; whoever resolves one passes the gate under their own access.
"""

from django.utils.translation import gettext as _

from apps.content import citation, reading
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA

ACCESS_SCHEMA = {
    "type": "object",
    "description": "The content gate's receipt for this caller.",
    "properties": {
        "tier": {"type": ["string", "null"], "enum": ["metadata", "outline", "snippet", "passage", "full", None]},
        "basis": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"]},
        "policy": {"type": "string", "description": "`key@version` of the content policy that decided."},
        "how_to_unlock": {"type": "array", "items": {"type": "string"}},
        "budget_left": {"type": ["integer", "null"]},
        "chars_charged": {"type": "integer"},
    },
    "required": ["tier", "basis", "policy", "how_to_unlock"],
}

CITATION_SCHEMA = {
    "type": "object",
    "properties": {
        "uri": {"type": "string"},
        "url": {"type": "string", "description": "A link a person opens: the viewer at the passage."},
        "kind": {"type": "string"},
        "work": {"type": "object", "description": "CSL-JSON of the publication."},
        "locator": {"type": "object", "description": "`page` (viewer), `source_page`, `section`, `label`."},
        "selector": {
            "type": "object",
            "description": "W3C TextQuoteSelector; only when the caller may read a snippet.",
        },
        "readium_locator": {"type": "object"},
        "resolution": {"type": "string", "enum": ["exact", "relocated", "page_only"]},
        "access": ACCESS_SCHEMA,
    },
    "required": ["uri", "url", "work", "locator", "readium_locator", "resolution", "access"],
}

ADDRESS = {
    "type": "string",
    "description": "evilflowers://entry/<id>/locator/<key> (from any content tool), evilflowers://entry/<id>, "
    "or a /cite/<id>/<key> link.",
}


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except reading.NotFound as error:
        raise ToolNotFound(error.kind, error.identifier) from error
    except reading.Invalid as error:
        raise ToolError(str(error)) from error


@registry.tool(
    name="search_content",
    title="Search inside publications",
    description=(
        "Find passages, figures and tables about a question across the library, or inside given publications. "
        "Each hit is an address (`uri`, and `url` — a link that opens the viewer at it) with the publication, "
        "page, score and, where your access allows a snippet, its text (`content`). Matches by meaning "
        "(multilingual) and by words. Read more around a hit with `read`, cite it with `cite` or "
        "`resolve_citation`. Use `search_entries` to find publications by their catalogue record instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "q": {"type": "string", "maxLength": 2000, "description": "What you are looking for."},
            "entry_ids": {"type": "array", "items": UUID_SCHEMA, "maxItems": 50},
            "catalog_id": UUID_SCHEMA,
            "kinds": {"type": "array", "items": {"type": "string", "enum": list(reading.KINDS)}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        "required": ["q"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "degraded": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["query", "degraded", "items"],
    },
)
def search_content(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("q", "entry_ids", "catalog_id", "kinds", "limit"))
    query = arguments.string("q", max_length=2000)
    if not query:
        raise ToolError(_("`q` is required."))
    kinds = arguments.enum_list("kinds", reading.KINDS)
    return _call(
        reading.search,
        request,
        query,
        entry_ids=arguments.uuid_sequence("entry_ids") or (),
        catalog_id=arguments.uuid("catalog_id"),
        kinds=tuple(kinds.split(",")) if kinds else reading.KINDS,
        limit=arguments.integer("limit", default=8, minimum=1, maximum=20),
    )


@registry.tool(
    name="get_outline",
    title="Get a publication's outline",
    description=(
        "The structure of a processed publication: its sections in reading order (address, heading, pages), "
        "its figures, tables and equations (address, printed label, caption where your access allows), its "
        "reference list when one was parsed, the page count and your access. Headings are withheld below the "
        "`outline` tier (e.g. Readium LCP titles you have not borrowed)."
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
            "title": {"type": "string"},
            "processed": {"type": "boolean"},
            "page_count": {"type": ["integer", "null"]},
            "sections": {"type": "array", "items": {"type": "object"}},
            "regions": {"type": "array", "items": {"type": "object"}},
            "references": {"type": "object"},
            "access": ACCESS_SCHEMA,
        },
        "required": ["entry_id", "processed", "sections", "regions", "access"],
    },
)
def get_outline(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_id",))
    return _call(reading.outline, request, arguments.uuid("entry_id", required=True))


@registry.tool(
    name="read",
    title="Read a publication",
    description=(
        "Read paragraphs in reading order from an address: a paragraph or section locator, a figure's (its "
        "page), or a whole publication (`evilflowers://entry/<id>`, from the start). Whole paragraphs only, each "
        "with its own address, page and sentence offsets; figures and tables on those pages come as "
        "`resource_link`s. `context` adds up to 3 paragraphs before the address; continue with `next_cursor`. "
        "Needs the `passage` tier: otherwise the result says why and how to unlock (e.g. borrow), with no text. "
        "Under a reading budget, paragraphs that do not fit are listed in `left_out`, never cut."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uri": ADDRESS,
            "context": {"type": "integer", "minimum": 0, "maximum": reading.MAX_CONTEXT, "default": 0},
            "cursor": {"type": "string", "description": "`next_cursor` of the previous call."},
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "maximum": reading.FULL_MAX_CHARS,
                "default": reading.DEFAULT_MAX_CHARS,
                "description": f"Characters to return; at most {reading.PASSAGE_MAX_CHARS} at the `passage` tier.",
            },
        },
        "required": ["uri"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "title": {"type": "string"},
            "paragraphs": {"type": "array", "items": {"type": "object"}},
            "regions": {"type": "array", "items": {"type": "object"}},
            "next_cursor": {"type": ["string", "null"]},
            "left_out": {"type": "array", "items": {"type": "object"}},
            "refused": {"type": "string"},
            "access": ACCESS_SCHEMA,
            "resource_links": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["entry_id", "paragraphs", "access"],
    },
)
def read(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("uri", "context", "cursor", "max_chars"))
    address = arguments.string("uri", max_length=500)
    if not address:
        raise ToolError(_("`uri` is required."))
    return _call(
        reading.read,
        request,
        address,
        context=arguments.integer("context", default=0, minimum=0, maximum=reading.MAX_CONTEXT),
        cursor=arguments.string("cursor", max_length=20),
        max_chars=arguments.integer(
            "max_chars", default=reading.DEFAULT_MAX_CHARS, minimum=200, maximum=reading.FULL_MAX_CHARS
        ),
    )


@registry.tool(
    name="get_region",
    title="Get a figure, table or equation",
    description=(
        "One figure, table or equation by its address: printed label and caption and a crop link (at the "
        "`snippet` tier), the model's description of a figure, and a table's Markdown or an equation's LaTeX "
        "(at the `passage` tier)."
    ),
    input_schema={
        "type": "object",
        "properties": {"uri": ADDRESS},
        "required": ["uri"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "uri": {"type": "string"},
            "url": {"type": "string"},
            "entry_id": {"type": "string"},
            "kind": {"type": "string"},
            "page": {"type": "integer"},
            "label": {"type": ["string", "null"]},
            "caption": {"type": "string"},
            "description": {"type": "string"},
            "markdown": {"type": "string"},
            "latex": {"type": "string"},
            "crop_url": {"type": "string"},
            "resolution": {"type": "string"},
            "access": ACCESS_SCHEMA,
        },
        "required": ["uri", "entry_id", "kind", "page", "access"],
    },
)
def get_region(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("uri",))
    address = arguments.string("uri", max_length=500)
    if not address:
        raise ToolError(_("`uri` is required."))
    return _call(reading.region, request, address)


@registry.tool(
    name="content_pack",
    title="Gather evidence for a question",
    description=(
        "In one call: the passages that best answer `query`, labelled `E1`, `E2`… with their addresses and "
        "`/cite` links, within `budget_chars`. Cite them by label and link in your answer. The pack is kept "
        "under `pack_id` (addresses only), so `resolve_citation(pack_id, label)` resolves a label later. "
        "Passages you may not read are listed in `withheld` with how to unlock them, never shown."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 2000},
            "purpose": {"type": "string", "maxLength": 500, "description": "What the evidence is for."},
            "budget_chars": {
                "type": "integer",
                "minimum": 500,
                "maximum": reading.MAX_PACK_CHARS,
                "default": reading.DEFAULT_PACK_CHARS,
            },
            "entry_ids": {"type": "array", "items": UUID_SCHEMA, "maxItems": 50},
            "catalog_id": UUID_SCHEMA,
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "pack_id": {"type": "string"},
            "query": {"type": "string"},
            "passages": {"type": "array", "items": {"type": "object"}},
            "withheld": {"type": "array", "items": {"type": "object"}},
            "used_chars": {"type": "integer"},
            "budget_chars": {"type": "integer"},
            "degraded": {"type": "boolean"},
        },
        "required": ["pack_id", "passages", "withheld"],
    },
)
def content_pack(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("query", "purpose", "budget_chars", "entry_ids", "catalog_id"))
    query = arguments.string("query", max_length=2000)
    if not query:
        raise ToolError(_("`query` is required."))
    return _call(
        reading.pack,
        request,
        query,
        purpose=arguments.string("purpose", max_length=500) or "",
        budget_chars=arguments.integer(
            "budget_chars", default=reading.DEFAULT_PACK_CHARS, minimum=500, maximum=reading.MAX_PACK_CHARS
        ),
        entry_ids=arguments.uuid_sequence("entry_ids") or (),
        catalog_id=arguments.uuid("catalog_id"),
    )


@registry.tool(
    name="resolve_citation",
    title="Resolve a citation",
    description=(
        "The citation object of an address or a `/cite` link — or of a label of a `content_pack` "
        "(`pack_id` + `label`): the work as CSL-JSON, the page (viewer and file numbering), section and "
        "label, the quote as a W3C TextQuoteSelector where your access allows a snippet, a Readium locator, "
        "how it resolved after re-processing (`exact`, `relocated`, `page_only`) and your access."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uri": ADDRESS,
            "pack_id": {"type": "string", "maxLength": 24},
            "label": {"type": "string", "maxLength": 10, "description": "`E3` of the pack."},
        },
        "additionalProperties": False,
    },
    output_schema=CITATION_SCHEMA,
)
def resolve_citation(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("uri", "pack_id", "label"))
    pack_id = arguments.string("pack_id", max_length=24)
    if pack_id:
        label = arguments.string("label", max_length=10)
        if not label:
            raise ToolError(_("`label` is required with `pack_id`."))
        return _call(reading.pack_citation, request, pack_id, label)
    address = arguments.string("uri", max_length=500)
    if not address:
        raise ToolError(_("Pass `uri`, or `pack_id` with `label`."))
    return _call(reading.citation_for, request, address)


@registry.tool(
    name="cite",
    title="Format references",
    description=(
        "Formatted references for addresses (from any content tool) in a citation style: `iso690` (ISO 690, "
        "as Slovak theses use), `apa`, `bibtex` or `csl-json`. Each reference carries the page and the `/cite` "
        "link to the passage."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uris": {"type": "array", "items": ADDRESS, "minItems": 1, "maxItems": 50},
            "style": {"type": "string", "enum": list(citation.STYLES), "default": "iso690"},
        },
        "required": ["uris"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "style": {"type": "string"},
            "references": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["style", "references"],
    },
)
def cite(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("uris", "style"))
    addresses = raw_arguments.get("uris") if isinstance(raw_arguments, dict) else None
    if not isinstance(addresses, list) or not addresses or not all(isinstance(item, str) for item in addresses):
        raise ToolError(_("`uris` must be a non-empty array of addresses."))
    style = arguments.enum("style", citation.STYLES, default="iso690")
    objects = [_call(reading.citation_for, request, address) for address in addresses[:50]]
    return {"style": style, "references": citation.cite(objects, style)}


@registry.tool(
    name="my_access",
    title="My access to a publication",
    description=(
        "How much of a publication's content you may see and why: tier, basis (open, loan, past loan, none), "
        "the content policy `key@version`, caps applied (outside the IP ranges, an external model provider, a "
        "TDM reservation), what is left of a reading budget and how to unlock more."
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
            "title": {"type": "string"},
            "tier": {"type": ["string", "null"]},
            "basis": {"type": ["string", "null"]},
            "reason": {"type": ["string", "null"]},
            "policy": {"type": "string"},
            "policy_source": {"type": "string"},
            "how_to_unlock": {"type": "array", "items": {"type": "string"}},
            "caps": {"type": "array", "items": {"type": "object"}},
            "budget": {"type": ["object", "null"]},
        },
        "required": ["entry_id", "tier", "basis", "policy", "how_to_unlock"],
    },
    access=ToolAccess.PUBLIC,
)
def my_access(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_id",))
    return _call(reading.my_access, request, arguments.uuid("entry_id", required=True))
