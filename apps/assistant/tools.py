"""The tool surface offered to the model.

IP-015 D1: the assistant does not define catalog tools of its own. It borrows
them from the MCP registry that IP-014 already built, tested and shipped, and
calls the handlers in-process — a tool is a plain `handler(request, arguments)`
callable, and JSON-RPC is a layer above that, not a prerequisite.

Only two catalog tools are advertised. The registry holds 25, but a small local
model's tool-calling degrades quickly as the list grows, and the reference
implementation did its job with three. Availability filtering needs no extra
tool: `search_entries` already accepts `lcp_states`, and its projections carry
LCP and shelf state, so "which of these can I borrow now?" is answerable from
the search result itself.
"""

from typing import List

from django.utils.translation import gettext as _

from apps.mcp.registry import Tool, ToolAccess, registry

#: Catalog tools the assistant may call, by registry name.
#:
#: Read-only by construction. This list is the security boundary: it is not
#: derived from `EVILFLOWERS_MCP_ALLOW_WRITE`, so enabling writes for MCP
#: clients never grants them to the chat assistant. A chat is driven by free
#: text from an end user and can be steered by text the catalog itself stores
#: (an entry summary, say), which is a far weaker position from which to allow
#: `delete_feed` than a deliberate MCP client is in.
ALLOWED_TOOLS = ("search_entries", "get_entry")

#: Name of the local tool that asks the UI to render publications.
DISPLAY_BOOKS = "displayBooks"

DISPLAY_BOOKS_SCHEMA = {
    "type": "object",
    "properties": {
        "entry_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Publication ids to render as cards, in the order they should appear.",
        }
    },
    "required": ["entry_ids"],
    "additionalProperties": False,
}

DISPLAY_BOOKS_DESCRIPTION = (
    "Show publications to the user as cards in the chat window. Call this with the ids returned "
    "by `search_entries` whenever you mention specific books, then keep your own reply short — "
    "do not repeat the titles and authors in prose, the cards already show them."
)


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


def specifications() -> List[dict]:
    """Every tool the model is told about, in Ollama's `/api/chat` format.

    The MCP descriptor and Ollama's tool schema differ only in shape, so this is
    a mechanical remap of `inputSchema` onto `function.parameters`.
    """
    specs = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in _catalog_tools()
    ]

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


def call(request, name: str, arguments: dict) -> dict:
    """Run a catalog tool and return its JSON-serialisable payload.

    `displayBooks` never reaches here — it is a UI directive with no catalog
    work behind it, and the view records it on the message instead.
    """
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

    return tool.handler(request, arguments or {})
