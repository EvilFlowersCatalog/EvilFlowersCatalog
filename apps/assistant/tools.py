"""The tool surface offered to the model.

IP-015 D1: the assistant does not define catalog tools of its own where the MCP
registry (IP-014) already has one. It borrows them and calls the handlers
in-process — a tool is a plain `handler(request, arguments)` callable, and
JSON-RPC is a layer above that, not a prerequisite.

IP-022 changes what the model is *told*, not what the handlers do:

- **Short descriptions and slim schemas.** The MCP descriptions are written for
  desktop clients on large models. On CPU inference every token of the tool
  list is evaluated on the first round of every chat (#104), so the assistant
  advertises its own short description and only the arguments it needs. The
  handler still accepts the full MCP argument set.
- **Slim results.** What a tool returns is replayed on every later round of the
  turn and of the conversation. `slim()` keeps what the model reasons with and
  drops URIs, ids of other objects and counters; the full payload is still
  stored on the message (`tool_result`).
- **Two discovery tools of its own**, wrapping functions the REST API already
  serves (`apps/extraction/discovery.py`): publications similar to one, and
  passages across the whole library.
- **Per-session list.** Tools about the reader (`get_my_shelf`, `list_my_loans`)
  are offered to authenticated sessions only. The list is fixed for a session,
  so it never changes inside a conversation's cached prefix.
- **Reading any open publication (#127).** A plain chat reads publications by
  id: `search_library_text` with an `entry_id` searches inside one publication
  (by meaning and words, which is what `search_paragraphs` and `search_pdf` do
  in a bound chat), and `read_pdf_page` reads its pages. Both follow
  `pdf.open_access_document`: never Readium LCP, never outside the entry's IP
  ranges. Pages read are cited like passages, through the evidence ledger.
"""

import json
from typing import Callable, Dict, List, Optional
from uuid import UUID

from django.conf import settings
from django.utils.translation import gettext as _

from apps.mcp.errors import ToolError
from apps.mcp.registry import Tool, ToolAccess, registry

#: Catalog tools the assistant may call, by registry name.
#:
#: Read-only by construction. This list is the security boundary: it is not
#: derived from `EVILFLOWERS_MCP_ALLOW_WRITE`, so enabling writes for MCP
#: clients never grants them to the chat assistant. A chat is driven by free
#: text from an end user and can be steered by text the catalog itself stores
#: (an entry summary, say), which is a far weaker position from which to allow
#: `delete_feed` than a deliberate MCP client is in.
ALLOWED_TOOLS = ("search_entries", "get_entry", "list_categories", "get_my_shelf", "list_my_loans")

#: Tools implemented here (read-only as well), by name.
FIND_SIMILAR = "find_similar"
# Not "search_passages": a bound chat also offers `search_paragraphs` (the reader's own copy),
# and two near-identical names are a coin toss for a small model.
SEARCH_PASSAGES = "search_library_text"
# Same name as the bound chat's tool over the reader's copy (`explain.READ_PDF_PAGE`): one verb, one name.
# A bound chat offers that one instead (`specifications(bound=True)`), so the two never appear together.
READ_PAGE = "read_pdf_page"
LOCAL_TOOLS = (FIND_SIMILAR, SEARCH_PASSAGES, READ_PAGE)

#: Local tools a chat bound to a reader's copy does not get: it has its own over that copy.
UNBOUND_ONLY = (READ_PAGE,)

#: The bound chat's other document tools (`explain.DOCUMENT_TOOLS`), which only read that chat's copy.
_OTHER_DOCUMENT_TOOLS = ("search_pdf", "list_page_regions", "get_region", "list_regions", "search_paragraphs")

#: Pages one `read_pdf_page` call returns (as in a bound chat).
READ_PAGE_MAX_COUNT = 3

#: Passages `search_library_text` returns from one publication (`entry_id`); the passage search caps it at 3.
PASSAGES_PER_PUBLICATION = 3

#: Name of the local tool that asks the UI to render publications.
DISPLAY_BOOKS = "displayBooks"

#: Results per search the model sees by default. The MCP default (10) is meant for
#: paging clients; the assistant shows a handful of cards and refines instead.
DEFAULT_SEARCH_LIMIT = 6
MAX_SEARCH_LIMIT = 15

#: Characters of a summary in a slim search hit (the MCP projection keeps 600).
SLIM_SUMMARY_CHARS = 280

_LCP_STATES = ["available_now", "available_in_days", "fully_borrowed", "active_loan_for_user", "not_lcp"]
_UUID_LIST = {"type": "array", "items": {"type": "string"}}

DISPLAY_BOOKS_SCHEMA = {
    "type": "object",
    "properties": {
        "entry_ids": {"type": "array", "items": {"type": "string"}, "description": "Publication ids, in order."}
    },
    "required": ["entry_ids"],
}

DISPLAY_BOOKS_DESCRIPTION = "Show publications as cards (title, author, availability)."

#: What the model is told about each tool: (description, parameters). Arguments
#: left out here are still accepted by the handler; they are only not advertised.
SPECIFICATIONS: Dict[str, tuple] = {
    "search_entries": (
        "Search the library's publications by meaning and words: id, title, authors, year, subjects, "
        "summary, availability.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Topic, title or keywords, any language."},
                "author": {"type": "string", "description": "Part of an author's name."},
                "category_term": {"type": "string", "description": "Exact term from `list_categories`."},
                "language_codes": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['sk']"},
                "published_from": {"type": "string", "description": "YYYY or YYYY-MM-DD"},
                "published_to": {"type": "string", "description": "YYYY or YYYY-MM-DD"},
                "lcp_states": {"type": "array", "items": {"type": "string", "enum": _LCP_STATES}},
                "order_by": {
                    "type": "string",
                    "enum": ["-published_at", "-created_at", "-popularity", "title"],
                    "description": "Omit for relevance.",
                },
                "limit": {"type": "integer", "description": f"1-{MAX_SEARCH_LIMIT}"},
            },
        },
    ),
    "get_entry": (
        "Full record of one publication: summary, table of contents, identifiers, availability.",
        {
            "type": "object",
            "properties": {"entry_id": {"type": "string", "description": "Publication id."}},
            "required": ["entry_id"],
        },
    ),
    "list_categories": (
        "The library's subjects (term, label): which subjects it covers, or a `category_term` to filter by.",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Part of a term or label; omit for all."}},
        },
    ),
    "get_my_shelf": (
        "Publications the reader saved to their shelf (bookmarks, not loans).",
        {"type": "object", "properties": {}},
    ),
    "list_my_loans": (
        "The reader's borrowed publications with the loan's start and expiry.",
        {"type": "object", "properties": {}},
    ),
    FIND_SIMILAR: (
        "Publications similar in content to one publication.",
        {
            "type": "object",
            "properties": {"entry_id": {"type": "string", "description": "Publication id."}},
            "required": ["entry_id"],
        },
    ),
    SEARCH_PASSAGES: (
        "Search inside publications' text for passages about a question: publication, page, passage. "
        "With `entry_id`, inside that one publication.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question or topic, in a sentence."},
                "entry_id": {"type": "string", "description": "Only this publication."},
                "category_term": {"type": "string", "description": "Only this subject."},
            },
            "required": ["query"],
        },
    ),
    READ_PAGE: (
        f"Read pages of a publication (at most {READ_PAGE_MAX_COUNT}).",
        {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "Publication id."},
                "page": {"type": "integer", "description": "First page, 1-based."},
                "count": {"type": "integer", "description": f"1-{READ_PAGE_MAX_COUNT}"},
            },
            "required": ["entry_id", "page"],
        },
    ),
}


class UnknownTool(Exception):
    """The model named a tool that is not on offer."""


class ToolNotPermitted(Exception):
    """The caller's session does not satisfy the tool's declared access level."""


def _catalog_tools() -> List[Tool]:
    # Tools register themselves on import. A web process imports them through the URLconf; a management
    # command (`assistant_eval`) or a Celery worker may never load it, and would find the registry empty.
    import apps.mcp.tools  # noqa: F401

    tools = []
    for name in ALLOWED_TOOLS:
        tool = registry.get(name)
        if tool is None:
            # A rename in apps/mcp must not silently shrink the assistant's
            # abilities; fail loudly at import/first use instead.
            raise UnknownTool(f"MCP tool '{name}' is not registered")
        if tool.writes:
            raise ToolNotPermitted(f"MCP tool '{name}' writes and may not be exposed to the assistant")
        tools.append(tool)
    return tools


def _offered(authenticated: bool, bound: bool = False) -> List[str]:
    """Tool names for a session, in a stable order (the order is part of the cached prompt).

    `bound`: the chat reads a reader's copy with the document tools (`explain.TOOL_SPECIFICATIONS`), which
    include their own `read_pdf_page`.
    """
    names = []
    for tool in _catalog_tools():
        if tool.access is ToolAccess.USER and not authenticated:
            continue
        names.append(tool.name)
    names.extend(name for name in LOCAL_TOOLS if not (bound and name in UNBOUND_ONLY))
    return names


def specifications(authenticated: bool = True, bound: bool = False) -> List[dict]:
    """Every tool the model is told about, in Ollama's `/api/chat` format."""
    specs = []
    for name in _offered(authenticated, bound):
        description, parameters = SPECIFICATIONS[name]
        specs.append(
            {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}
        )

    specs.append(
        {
            "type": "function",
            "function": {
                "name": DISPLAY_BOOKS,
                "description": DISPLAY_BOOKS_DESCRIPTION,
                "parameters": DISPLAY_BOOKS_SCHEMA,
            },
        }
    )
    return specs


def is_display_books(name: str) -> bool:
    return name == DISPLAY_BOOKS


def clean_arguments(arguments) -> dict:
    """Drop the empty values small models fill optional arguments with (null, "", [])."""
    if not isinstance(arguments, dict):
        return {}
    return {key: value for key, value in arguments.items() if value not in (None, "", [], {})}


class Reading:
    """Pages read during one turn, against one budget (`EVILFLOWERS_ASSISTANT_MAX_PAGES_READ`, IP-018 Q5).

    `bound` is the turn's list of pages read from the chat's own copy (it feeds the `pages` frame); `books`
    holds pages read from other publications by id (#127). A page already read is free to read again.
    """

    def __init__(self, bound: Optional[List[int]] = None):
        self.bound: List[int] = bound if bound is not None else []
        self.books: Dict[str, List[int]] = {}

    def pages_for(self, entry_id: str) -> List[int]:
        return self.books.setdefault(entry_id, [])

    def budget_for(self, entry_id: str) -> int:
        """The budget `run_document_tool` checks this publication's own list against."""
        others = len(self.bound) + sum(len(pages) for key, pages in self.books.items() if key != entry_id)
        return max(settings.EVILFLOWERS_ASSISTANT_MAX_PAGES_READ - others, 0)


def call(request, name: str, arguments: dict, reading: Optional[Reading] = None) -> dict:
    """Run a tool and return its full JSON-serialisable payload.

    `displayBooks` never reaches here — it is a UI directive with no catalog
    work behind it, and the view records it on the message instead.
    Raises `ToolError` (from `apps.mcp`) with a message meant for the model.
    """
    arguments = clean_arguments(arguments)

    if name == READ_PAGE:
        return _read_page(request, arguments, reading if reading is not None else Reading())
    if name in LOCAL_TOOLS:
        return _LOCAL[name](request, arguments)

    if name in _OTHER_DOCUMENT_TOOLS:
        # A bound chat's document tool aimed at another publication (`turn._another_publication`).
        raise ToolError(
            f"`{name}` reads the publication this chat is about. For another publication use "
            f"`{SEARCH_PASSAGES}` with its `entry_id`, or `{READ_PAGE}`."
        )

    if name not in ALLOWED_TOOLS:
        raise UnknownTool(name)

    tool = registry.get(name)
    if tool is None:
        raise UnknownTool(name)

    # The declared access level is enforced before the handler runs, exactly as
    # the MCP server does it — a handler is not responsible for checking, so
    # calling one directly without this check would bypass it entirely.
    if tool.access is not ToolAccess.PUBLIC and not request.user.is_authenticated:
        raise ToolNotPermitted(_("`%(tool)s` requires an authenticated session.") % {"tool": name})

    if name == "search_entries":
        try:
            limit = int(arguments.get("limit") or DEFAULT_SEARCH_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_SEARCH_LIMIT
        arguments["limit"] = max(1, min(limit, MAX_SEARCH_LIMIT))
    elif name == "list_categories":
        return _every_page(tool, request, arguments)

    return tool.handler(request, arguments)


#: Pages `list_categories` is walked through at most (× EVILFLOWERS_MCP_MAX_LIMIT terms).
MAX_CATEGORY_PAGES = 4


def _every_page(tool: Tool, request, arguments: dict) -> dict:
    """The whole vocabulary in one tool call (STU: 59 terms, more than one MCP page).

    The model asked "which subjects": half a list would be a wrong answer, and the slim
    result is only term + label, so the whole list stays small.
    """
    items, page = [], None
    for number in range(1, MAX_CATEGORY_PAGES + 1):
        page = tool.handler(request, {**arguments, "limit": settings.EVILFLOWERS_MCP_MAX_LIMIT, "page": number})
        items.extend(page.get("items") or [])
        if not (page.get("metadata") or {}).get("has_next_page"):
            break
    total = (page.get("metadata") or {}).get("total", len(items))
    return {"items": items, "metadata": {"total": total, "limit": len(items), "has_next_page": len(items) < total}}


def run(request, name: str, arguments: dict, ledger=None, reading: Optional[Reading] = None) -> tuple:
    """Run a tool for the chat: `(payload or None, text the model sees)`.

    `ledger` is the chat's evidence ledger (IP-024): passages of `search_library_text` and pages of
    `read_pdf_page` get labels from it that the model cites in its answer. Without one, they are unlabelled.
    `reading` is the turn's page budget (#127).
    """
    try:
        payload = call(request, name, arguments, reading)
    except (UnknownTool, ToolNotPermitted) as error:
        # Hand the refusal to the model verbatim so it can correct itself
        # instead of failing the whole turn.
        return None, json.dumps({"error": str(error) or f"Unknown tool: {name}"})
    except ToolError as error:
        # Validation and not-found messages are written for a model ("Unknown
        # argument(s): …. Accepted arguments: …"), so they are the best hint it can get.
        return None, json.dumps({"error": str(error)})
    if ledger is not None and isinstance(payload, dict):
        from apps.assistant.evidence import label_pages, label_passages

        if name == SEARCH_PASSAGES:
            label_passages(payload, ledger)
        elif name == READ_PAGE:
            label_pages(payload, ledger)
    return payload, json.dumps(slim(name, payload), ensure_ascii=False, default=str)


# ----------------------------------------------------------------- local tools


def _uuid(value) -> Optional[str]:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _resolve_entries(request, entry_ids: List) -> Dict[str, dict]:
    """Slim records of these entries, keyed by id, limited to what the caller may see."""
    if not entry_ids:
        return {}

    from django.http import QueryDict

    from apps.api.filters.entries import EntryFilter
    from apps.api.views.entries import shelf_record_mapping
    from apps.core.models import Entry
    from apps.mcp.projections import entry_summary
    from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

    params = QueryDict(mutable=True)
    params["id"] = ",".join(str(entry_id) for entry_id in entry_ids)
    entries = list(
        EntryFilter(params, queryset=Entry.objects.all(), request=request)
        .qs.select_related("language")
        .prefetch_related("entry_authors__author", "categories")
        .distinct()
    )
    lcp_states = lcp_state_mapping(request.user, entries)
    shelf = shelf_record_mapping(request.user) if request.user.is_authenticated else {}
    return {str(entry.pk): entry_summary(entry, request, lcp_states, shelf) for entry in entries}


def _find_similar(request, arguments: dict) -> dict:
    from apps.extraction.discovery import _visible, similar_entries

    entry_id = _uuid(arguments.get("entry_id"))
    if entry_id is None:
        raise ToolError("`entry_id` must be a publication id from a tool result.")
    if UUID(entry_id) not in _visible(request, [entry_id]):
        raise ToolError(f"No publication {entry_id} is visible to this reader.")

    similar = similar_entries(request, entry_id, DEFAULT_SEARCH_LIMIT)
    records = _resolve_entries(request, [item["entry_id"] for item in similar])
    items = []
    for item in similar:
        record = records.get(str(item["entry_id"]))
        if record is not None:
            items.append({**record, "similarity": item["score"]})
    return {"items": items, "metadata": {"total": len(items)}}


#: What the model is told when it may not read a publication's text, by `pdf.DocumentUnavailable` reason.
#: The Readium wording matches `services.entry_note`: the reader borrows the publication and reads it in Thorium.
_UNREADABLE = {
    "readium": "This publication is lent through Readium LCP, so its text cannot be read here. Answer from its "
    "catalogue record, and tell the reader to borrow it to read it.",
    "ip_blocked": "This publication may only be read from the library's network.",
}
_NOT_READABLE = "This publication has no text that can be read here (no open PDF). Answer from its catalogue record."


def _publication(request, entry_id_argument):
    """The entry the model named, if this reader may see it; `ToolError` otherwise."""
    from apps.core.models import Entry
    from apps.extraction.discovery import _visible

    entry_id = _uuid(entry_id_argument)
    if entry_id is None:
        raise ToolError("`entry_id` must be a publication id from a tool result.")
    if UUID(entry_id) not in _visible(request, [entry_id]):
        raise ToolError(f"No publication {entry_id} is visible to this reader.")
    return Entry.objects.get(pk=entry_id)


def _document(request, entry):
    """The publication's readable PDF (`pdf.open_access_document`), or a `ToolError` saying why not."""
    from apps.assistant.pdf import DocumentUnavailable, open_access_document

    try:
        return open_access_document(request, entry)
    except DocumentUnavailable as error:
        reason = error.args[0] if error.args else ""
        raise ToolError(_UNREADABLE.get(reason, _NOT_READABLE)) from error


def _read_page(request, arguments: dict, reading: Reading) -> dict:
    """Pages of any publication the reader may read (#127); the bound chat's own copy has its own tool."""
    from apps.assistant.explain import READ_PDF_PAGE, run_document_tool

    entry = _publication(request, arguments.get("entry_id"))
    document = _document(request, entry)
    entry_id = str(entry.pk)
    call = {
        "function": {
            "name": READ_PDF_PAGE,
            "arguments": {"page": arguments.get("page"), "count": arguments.get("count") or 1},
        }
    }
    result = json.loads(run_document_tool(document, call, reading.pages_for(entry_id), reading.budget_for(entry_id)))
    if "error" in result:
        raise ToolError(result["error"])
    return {
        "entry_id": entry_id,
        "title": entry.title,
        "page_count": document.page_count,
        "pages": result.get("pages") or [],
    }


def _search_passages(request, arguments: dict) -> dict:
    from apps.extraction.discovery import search_passages

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ToolError("`query` is required.")
    scope = {}
    if arguments.get("category_term"):
        scope["category_term"] = str(arguments["category_term"])
    if arguments.get("entry_id"):
        return _search_publication(request, query, _publication(request, arguments["entry_id"]))

    result = search_passages(request, query, limit=5, per_entry=2, scope=scope)
    if result.degraded and not result.items:
        # Search by meaning is down and no passage has the query's words (#113).
        return {"items": [], "note": "Passage search is unavailable right now; use search_entries."}

    payload = {"items": _passage_items(request, result), "searched_publications": result.indexed}
    if not payload["items"]:
        payload["note"] = "No passage matched. Try search_entries, or other words."
    return payload


def _search_publication(request, query: str, entry) -> dict:
    """Passages of one publication (#127); word matches by page where it has no passage index."""
    from apps.extraction.discovery import search_passages

    entry_id = str(entry.pk)
    result = search_passages(request, query, limit=1, per_entry=PASSAGES_PER_PUBLICATION, scope={"id": entry_id})
    items = _passage_items(request, result)
    if items:
        payload = {"items": items}
        if entry.read_config("readium_enabled"):
            payload["note"] = _UNREADABLE["readium"]
        return payload

    # Not processed into paragraphs (or nothing matched there): the page text itself, as `search_pdf` reads it.
    document = _document(request, entry)
    hits = document.search(query, limit=5)
    payload = {"entry_id": entry_id, "title": entry.title, "items": []}
    if hits:
        payload["matches"] = hits
        payload["note"] = "Word matches by page. Read the best pages with read_pdf_page before answering."
    else:
        payload["note"] = "Nothing in this publication matches. Try other words, or read its contents (get_entry)."
    return payload


def _passage_items(request, result) -> List[dict]:
    """A passage search result as the tool's items: publication, title and passages."""
    titles = {
        entry_id: record.get("title")
        for entry_id, record in _resolve_entries(request, [item.entry_id for item in result.items]).items()
    }
    items = []
    for hit in result.items:
        items.append(
            {
                "entry_id": str(hit.entry_id),
                "title": titles.get(str(hit.entry_id)),
                "passages": [
                    {
                        key: value
                        for key, value in (
                            # Kept in the full payload so a passage found again keeps its evidence
                            # label (IP-024); `slim` drops it from the model's view.
                            ("paragraph_id", str(passage.paragraph_id) if passage.paragraph_id else None),
                            ("page", passage.page),
                            ("section", passage.section),
                            ("score", passage.score),
                            ("text", passage.text),
                        )
                        if value not in (None, "")
                    }
                    for passage in hit.passages
                ],
            }
        )
    return items


_LOCAL: Dict[str, Callable] = {FIND_SIMILAR: _find_similar, SEARCH_PASSAGES: _search_passages}


# ----------------------------------------------------------------- slim results

#: Keys of an entry summary the model reasons with; the rest is for API clients.
_ENTRY_KEYS = ("id", "title", "authors", "published_at", "language", "categories", "summary", "rating")
_AVAILABILITY_KEYS = ("state", "available_slots", "total_slots", "next_available_at", "user_queue_position")


def _slim_entry(record: dict) -> dict:
    slim_record = {key: record[key] for key in _ENTRY_KEYS if record.get(key) not in (None, "", [])}
    published = slim_record.pop("published_at", None)
    if published:
        slim_record["year"] = str(published)[:4]
    summary = slim_record.get("summary")
    if summary and len(summary) > SLIM_SUMMARY_CHARS:
        slim_record["summary"] = summary[:SLIM_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    availability = record.get("availability")
    if availability:
        slim_record["availability"] = {
            key: availability[key] for key in _AVAILABILITY_KEYS if availability.get(key) is not None
        }
    if record.get("on_my_shelf"):
        slim_record["on_my_shelf"] = True
    if record.get("similarity") is not None:
        slim_record["similarity"] = record["similarity"]
    return slim_record


def _pick(record: dict, keys) -> dict:
    return {key: record[key] for key in keys if record.get(key) not in (None, "", [])}


def _total(payload: dict, items: list) -> int:
    return (payload.get("metadata") or {}).get("total", len(items))


def _slim_entries(payload: dict) -> dict:
    items = [_slim_entry(item) for item in payload.get("items") or []]
    slim_payload = {"total": _total(payload, items), "items": items}
    if not items:
        slim_payload["note"] = "Nothing found. Try other or English words, fewer filters, or search_library_text."
    return slim_payload


_ENTRY_DETAIL_DROP = (
    "catalog_id",
    "resource_uri",
    "cover_url",
    "acquisitions",
    "created_at",
    "updated_at",
    "url_name",
)


def _slim_entry_detail(payload: dict) -> dict:
    from apps.assistant.chapters import outline_for_model

    entry = {key: value for key, value in (payload.get("entry") or {}).items() if key not in _ENTRY_DETAIL_DROP}
    if entry.get("availability"):
        entry["availability"] = _pick(entry["availability"], _AVAILABILITY_KEYS)
    if entry.get("id"):
        # Chapter summaries, when the publication has them, replace the bare outline (#119).
        entry.update(outline_for_model(entry["id"], entry.pop("table_of_contents", None)))
    return {"entry": entry}


def _slim_shelf(payload: dict) -> dict:
    return {"items": [_slim_entry(item.get("entry") or {}) for item in payload.get("items") or []]}


_LOAN_KEYS = ("entry_id", "entry_title", "state", "starts_at", "expires_at", "renewal_count")


def _slim_loans(payload: dict) -> dict:
    return {"items": [_pick(item, _LOAN_KEYS) for item in payload.get("items") or []]}


def _slim_categories(payload: dict) -> dict:
    items = [_pick(item, ("term", "label")) for item in payload.get("items") or []]
    return {"total": _total(payload, items), "items": items}


def _slim_passages(payload: dict) -> dict:
    """Passages without the bookkeeping: the evidence map and paragraph ids are for resolving citations."""
    from apps.assistant.evidence import EVIDENCE_KEY

    slim_payload = {key: value for key, value in payload.items() if key != EVIDENCE_KEY}
    slim_payload["items"] = [
        {
            **item,
            "passages": [
                {key: value for key, value in passage.items() if key != "paragraph_id"}
                for passage in item.get("passages") or []
            ],
        }
        for item in payload.get("items") or []
    ]
    return slim_payload


def _slim_pages(payload: dict) -> dict:
    """Pages read, with their labels; the evidence map is for resolving citations."""
    from apps.assistant.evidence import EVIDENCE_KEY

    return {key: value for key, value in payload.items() if key != EVIDENCE_KEY}


_SLIMMERS: Dict[str, Callable[[dict], dict]] = {
    "search_entries": _slim_entries,
    FIND_SIMILAR: _slim_entries,
    SEARCH_PASSAGES: _slim_passages,
    READ_PAGE: _slim_pages,
    "get_entry": _slim_entry_detail,
    "get_my_shelf": _slim_shelf,
    "list_my_loans": _slim_loans,
    "list_categories": _slim_categories,
}


def slim(name: str, payload) -> dict:
    """What the model sees of a tool result (see the module docstring)."""
    slimmer = _SLIMMERS.get(name)
    if slimmer is None or not isinstance(payload, dict):
        return payload
    return slimmer(payload)
