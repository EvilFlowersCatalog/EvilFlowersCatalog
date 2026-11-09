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
LOCAL_TOOLS = (FIND_SIMILAR, SEARCH_PASSAGES)

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
        "entry_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Publication ids from a tool result, in display order.",
        }
    },
    "required": ["entry_ids"],
}

DISPLAY_BOOKS_DESCRIPTION = (
    "Show publications to the reader as cards. Call it with the ids of the publications you recommend; "
    "the cards show title, author and availability, so do not repeat those in your reply."
)

#: What the model is told about each tool: (description, parameters). Arguments
#: left out here are still accepted by the handler; they are only not advertised.
SPECIFICATIONS: Dict[str, tuple] = {
    "search_entries": (
        "Search the library's publications by meaning and words. Returns compact records with id, title, "
        "authors, year, language, subjects, a short summary and borrowing availability.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Topic, title or keywords. Matches by meaning too, in any language.",
                },
                "author": {"type": "string", "description": "Part of an author's name."},
                "category_term": {
                    "type": "string",
                    "description": "Exact subject term, as `list_categories` or a search result gives it.",
                },
                "language_codes": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['sk']"},
                "published_from": {"type": "string", "description": "YYYY, YYYY-MM or YYYY-MM-DD"},
                "published_to": {"type": "string", "description": "YYYY, YYYY-MM or YYYY-MM-DD"},
                "lcp_states": {
                    "type": "array",
                    "items": {"type": "string", "enum": _LCP_STATES},
                    "description": "Borrowing availability; available_now = can be borrowed right now.",
                },
                "order_by": {
                    "type": "string",
                    "enum": ["-published_at", "-created_at", "-popularity", "title"],
                    "description": "Omit to rank by relevance.",
                },
                "limit": {"type": "integer", "description": f"1-{MAX_SEARCH_LIMIT}, default {DEFAULT_SEARCH_LIMIT}"},
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
        "List the library's subject categories (term and label). Use it to find the exact `category_term` "
        "for a subject, or to say which subjects the library covers.",
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
        "Publications similar in content to one publication. Use it for 'something like this' and for "
        "alternatives when a publication is fully borrowed.",
        {
            "type": "object",
            "properties": {"entry_id": {"type": "string", "description": "Publication id."}},
            "required": ["entry_id"],
        },
    ),
    SEARCH_PASSAGES: (
        "Search inside the text of every processed publication for passages about a question; returns the "
        "publication, page and passage. Use it for detailed questions that titles and summaries do not "
        "answer. Text of DRM-protected publications is withheld (only the page is given).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question or topic, in a sentence."},
                "category_term": {"type": "string", "description": "Optional: only this subject."},
            },
            "required": ["query"],
        },
    ),
}


class UnknownTool(Exception):
    """The model named a tool that is not on offer."""


class ToolNotPermitted(Exception):
    """The caller's session does not satisfy the tool's declared access level."""


def _catalog_tools() -> List[Tool]:
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


def _offered(authenticated: bool) -> List[str]:
    """Tool names for a session, in a stable order (the order is part of the cached prompt)."""
    names = []
    for tool in _catalog_tools():
        if tool.access is ToolAccess.USER and not authenticated:
            continue
        names.append(tool.name)
    names.extend(LOCAL_TOOLS)
    return names


def specifications(authenticated: bool = True) -> List[dict]:
    """Every tool the model is told about, in Ollama's `/api/chat` format."""
    specs = []
    for name in _offered(authenticated):
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


def call(request, name: str, arguments: dict) -> dict:
    """Run a tool and return its full JSON-serialisable payload.

    `displayBooks` never reaches here — it is a UI directive with no catalog
    work behind it, and the view records it on the message instead.
    Raises `ToolError` (from `apps.mcp`) with a message meant for the model.
    """
    arguments = clean_arguments(arguments)

    if name in LOCAL_TOOLS:
        return _LOCAL[name](request, arguments)

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


def run(request, name: str, arguments: dict) -> tuple:
    """Run a tool for the chat: `(payload or None, text the model sees)`."""
    try:
        payload = call(request, name, arguments)
    except (UnknownTool, ToolNotPermitted) as error:
        # Hand the refusal to the model verbatim so it can correct itself
        # instead of failing the whole turn.
        return None, json.dumps({"error": str(error) or f"Unknown tool: {name}"})
    except ToolError as error:
        # Validation and not-found messages are written for a model ("Unknown
        # argument(s): …. Accepted arguments: …"), so they are the best hint it can get.
        return None, json.dumps({"error": str(error)})
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


def _search_passages(request, arguments: dict) -> dict:
    from apps.extraction.discovery import search_passages

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ToolError("`query` is required.")
    scope = {}
    if arguments.get("category_term"):
        scope["category_term"] = str(arguments["category_term"])

    result = search_passages(request, query, limit=5, per_entry=2, scope=scope)
    if result.degraded:
        return {"items": [], "note": "Passage search is unavailable right now; use search_entries."}

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
    payload = {"items": items, "searched_publications": result.indexed}
    if not items:
        payload["note"] = "No passage matched. Try search_entries, or other words."
    return payload


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
    entry = {key: value for key, value in (payload.get("entry") or {}).items() if key not in _ENTRY_DETAIL_DROP}
    if entry.get("availability"):
        entry["availability"] = _pick(entry["availability"], _AVAILABILITY_KEYS)
    return {"entry": entry}


def _slim_shelf(payload: dict) -> dict:
    return {"items": [_slim_entry(item.get("entry") or {}) for item in payload.get("items") or []]}


_LOAN_KEYS = ("entry_id", "entry_title", "state", "starts_at", "expires_at", "renewal_count")


def _slim_loans(payload: dict) -> dict:
    return {"items": [_pick(item, _LOAN_KEYS) for item in payload.get("items") or []]}


def _slim_categories(payload: dict) -> dict:
    items = [_pick(item, ("term", "label")) for item in payload.get("items") or []]
    return {"total": _total(payload, items), "items": items}


_SLIMMERS: Dict[str, Callable[[dict], dict]] = {
    "search_entries": _slim_entries,
    FIND_SIMILAR: _slim_entries,
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
