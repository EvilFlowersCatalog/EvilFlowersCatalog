"""Dispatch one JSON-RPC message to an MCP method.

Stateless by construction: nothing is remembered between messages, so there is
no session to create, resume or expire. The Streamable HTTP transport permits
this — `Mcp-Session-Id` is optional — and it means the endpoint scales the same
way every other view in this project does, behind the same gunicorn workers.

**Access enforcement lives here, not in the handlers.** Every tool declares a
`ToolAccess` level and this module checks it before the handler runs, so a new
tool cannot forget to authenticate. What a handler still owns is object-level
permission — whether *this* user may manage *that* catalog — because only the
handler knows which object is in play.
"""

import json
import logging
from typing import List, Optional

from django.conf import settings
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.mcp import completions, prompts, resources
from apps.mcp.errors import ToolError
from apps.mcp.metadata import credential_hint
from apps.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    JsonRpcError,
    failure,
    negotiate_protocol_version,
    success,
    supports_structured_output,
)
from apps.mcp.registry import Tool, ToolAccess, ToolRegistry

logger = logging.getLogger("apps.mcp.server")
audit = logging.getLogger("apps.mcp.audit")

READ_INSTRUCTIONS = (
    "This server exposes an OPDS publication catalog — a digital library.\n\n"
    "Start with `search_entries` for any question about what the library holds; it combines "
    "free-text search with structured filters and returns compact records including live "
    "borrowing availability. When a name, subject or collection is uncertain, resolve it first "
    "with `list_authors`, `list_categories`, `list_catalogs` or `list_feeds` and filter by the id "
    "you get back — that is far more reliable than guessing at a spelling. Use `get_entry` to "
    "read one publication in full.\n\n"
    "Results are always scoped to what the calling credential may read; an unauthenticated "
    "session sees public catalogs only. `whoami`, `get_my_shelf` and `list_my_loans` describe the "
    "authenticated user's own library and require credentials."
)

WRITE_INSTRUCTIONS = (
    "\n\nThis deployment also lets you curate the library: catalogs, feeds and categories can be "
    "created, updated and deleted, publications can be filed under categories "
    "(`classify_entries`) and moved in and out of feeds (`add_entries_to_feed`, "
    "`remove_entries_from_feed`).\n\n"
    "Every management tool needs `manage` access on the catalog in question. Start with "
    "`list_catalogs` — each result carries an `access` level — or `whoami` for the count. "
    "Creating a *catalog* additionally requires administrator rights, which `manage` on an "
    "existing catalog does not confer.\n\n"
    "Survey before you write. `list_feeds` and `list_categories` first, so you extend the "
    "existing structure rather than duplicating it, and note that everything linked in one write "
    "must live in the same catalog — a category from catalog A cannot be applied to a "
    "publication in catalog B.\n\n"
    "Bulk tools exist so cataloguing a thousand publications is not a thousand calls: "
    "`create_categories` imports a whole vocabulary at once and skips terms that already exist, "
    "and `classify_entries` files a batch of publications in one go. Both report per record what "
    "actually changed, so re-running them is safe. Note that nothing here classifies *for* you — "
    "`classify_entries` records the decision you make from each publication's own metadata.\n\n"
    "Be careful in the other direction too. `update_feed` and `update_category` change only the "
    "arguments you pass, but passing `entry_ids` or `parent_ids` replaces that entire set — "
    'prefer `add_entries_to_feed` when curating. `classify_entries` with `mode: "replace"` '
    "discards classifications you did not name; `add` is the default for that reason. Deletions "
    "cannot be undone: confirm with the user first, note that `delete_category` reports how many "
    "publications lost the classification, and treat `delete_catalog` — which destroys every "
    "publication and file inside it — as something to propose, never to volunteer."
)

READ_ONLY_NOTE = "\n\nThis deployment is read-only: no tool here borrows, reserves, edits or deletes."


class McpServer:
    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    @property
    def write_enabled(self) -> bool:
        return bool(settings.EVILFLOWERS_MCP_ALLOW_WRITE)

    # -- entry point ----------------------------------------------------

    def handle(self, request, message, protocol_version: str = ""):
        """Return a JSON-RPC response object, or `None` for a notification.

        JSON-RPC forbids answering a notification (a message with no `id`), so
        `None` here means "the caller should send nothing back for this one".
        """
        if not isinstance(message, dict):
            return failure(None, JsonRpcError(INVALID_REQUEST, "A JSON-RPC message must be an object."))

        if message.get("jsonrpc") != JSONRPC_VERSION:
            return failure(
                message.get("id"),
                JsonRpcError(INVALID_REQUEST, f"Unsupported JSON-RPC version; expected '{JSONRPC_VERSION}'."),
            )

        method = message.get("method")
        is_notification = "id" not in message

        # We never send requests, so anything arriving without a `method` is
        # either a stray response or malformed.
        if not isinstance(method, str):
            if is_notification:
                return None
            return failure(message.get("id"), JsonRpcError(INVALID_REQUEST, "Missing `method`."))

        if is_notification:
            self._handle_notification(method)
            return None

        try:
            result = self._invoke(request, method, message.get("params") or {}, protocol_version)
            return success(message["id"], result)
        except JsonRpcError as error:
            return failure(message["id"], error)
        except ToolError as error:
            # A resource or prompt failure — these have no `isError` result
            # shape of their own, so they surface as protocol errors.
            return failure(message["id"], JsonRpcError(INVALID_PARAMS, str(error)))
        except Exception:
            logger.exception("Unhandled error while dispatching MCP method '%s'", method)
            return failure(message["id"], JsonRpcError(INTERNAL_ERROR, _("Internal server error.")))

    # -- methods --------------------------------------------------------

    def _invoke(self, request, method: str, params, protocol_version: str):
        if not isinstance(params, dict):
            raise JsonRpcError(INVALID_PARAMS, "`params` must be an object.")

        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._registry.descriptors(write_enabled=self.write_enabled)}
        if method == "tools/call":
            return self._call_tool(request, params, protocol_version)
        if method == "resources/list":
            return resources.list_resources(request, params.get("cursor"))
        if method == "resources/templates/list":
            return resources.list_resource_templates(request)
        if method == "resources/read":
            return resources.read_resource(request, params.get("uri"))
        if method == "prompts/list":
            return prompts.list_prompts()
        if method == "prompts/get":
            return prompts.get_prompt(params.get("name"), params.get("arguments"))
        if method == "completion/complete":
            return completions.complete(request, params.get("ref"), params.get("argument"))

        raise JsonRpcError(METHOD_NOT_FOUND, f"Unknown method '{method}'.")

    def _handle_notification(self, method: str) -> None:
        # `notifications/initialized` and `notifications/cancelled` are the only
        # ones a client sends us. There is nothing to tear down for a
        # cancellation — every tool call is a synchronous request/response — so
        # all notifications are acknowledged by silence.
        logger.debug("Ignoring MCP notification '%s'", method)

    def _initialize(self, params: dict) -> dict:
        client = params.get("clientInfo") or {}
        logger.info(
            "MCP initialize from client=%s version=%s protocol=%s",
            client.get("name", "unknown"),
            client.get("version", "unknown"),
            params.get("protocolVersion", "unspecified"),
        )

        instructions = READ_INSTRUCTIONS + (WRITE_INSTRUCTIONS if self.write_enabled else READ_ONLY_NOTE)

        return {
            "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
            "capabilities": {
                "tools": {"listChanged": False},
                # No subscriptions: a stateless server has no channel on which
                # to deliver an update notification.
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
                "completions": {},
            },
            "serverInfo": {
                "name": "evilflowers-catalog",
                "title": settings.INSTANCE_NAME,
                "version": settings.VERSION,
            },
            "instructions": instructions,
        }

    # -- tools ----------------------------------------------------------

    def _resolve_tool(self, name) -> Tool:
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "`name` is required and must be a string.")

        tool = self._registry.get(name)
        if tool is None or (tool.writes and not self.write_enabled):
            available = ", ".join(
                descriptor["name"] for descriptor in self._registry.descriptors(write_enabled=self.write_enabled)
            )
            # A write tool on a read-only deployment reports as unknown rather
            # than forbidden: it was never advertised, and saying "disabled"
            # invites a model to keep trying.
            raise JsonRpcError(INVALID_PARAMS, f"Unknown tool '{name}'. Available tools: {available}.")

        return tool

    def _authorize(self, request, tool: Tool) -> Optional[dict]:
        """Enforce the tool's declared access level. `None` means "go ahead"."""
        if tool.access is ToolAccess.PUBLIC:
            return None

        if not request.user.is_authenticated:
            if tool.writes:
                return self._error_result(
                    _(
                        "`%(tool)s` changes catalog data and requires an authenticated session with "
                        "`manage` access on the catalog. Connect with %(hint)s."
                    )
                    % {"tool": tool.name, "hint": credential_hint()}
                )
            return self._error_result(
                _(
                    "`%(tool)s` reports on the signed-in user's own library and needs credentials. "
                    "Connect with %(hint)s, or use `search_entries` to browse public catalogs instead."
                )
                % {"tool": tool.name, "hint": credential_hint()}
            )

        return None

    def _call_tool(self, request, params: dict, protocol_version: str) -> dict:
        tool = self._resolve_tool(params.get("name"))

        refusal = self._authorize(request, tool)
        if refusal is not None:
            audit.info(
                "mcp.denied user=anonymous tool=%s reason=authentication_required",
                tool.name,
            )
            return refusal

        try:
            payload = tool.handler(request, params.get("arguments"))
        except ToolError as error:
            # Expected and explainable — hand the message to the model verbatim
            # so it can correct the call itself.
            if tool.writes:
                audit.info("mcp.refused user=%s tool=%s reason=%s", request.user.pk, tool.name, error)
            return self._error_result(str(error))
        except ProblemDetailException as error:
            logger.warning("MCP tool '%s' rejected the call: %s", tool.name, error.title)
            return self._error_result(f"{error.title}{f' — {error.detail}' if error.detail else ''}")
        except Exception:
            logger.exception("MCP tool '%s' raised", tool.name)
            return self._error_result(
                _("`%(tool)s` failed unexpectedly. The failure has been logged.") % {"tool": tool.name}
            )

        return self._tool_result(payload, protocol_version)

    def _tool_result(self, payload: dict, protocol_version: str) -> dict:
        content: List[dict] = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]

        if supports_structured_output(protocol_version):
            content.extend(self._resource_links(payload))
            return {"content": content, "structuredContent": payload, "isError": False}

        # Pre-2025-06-18: the text block is the whole result.
        return {"content": content, "isError": False}

    @staticmethod
    def _resource_links(payload: dict) -> List[dict]:
        """Turn `resource_uri` fields in a result into `resource_link` blocks.

        Lets a client offer "attach this publication" next to a search hit
        without the model having to call anything else. Capped so a full page of
        results cannot bury the text block.
        """
        candidates = []
        if isinstance(payload.get("items"), list):
            candidates = payload["items"]
        else:
            for value in payload.values():
                if isinstance(value, dict) and "resource_uri" in value:
                    candidates = [value]
                    break

        links = []
        for item in candidates[:25]:
            if not isinstance(item, dict):
                continue
            uri = item.get("resource_uri") or (item.get("entry") or {}).get("resource_uri")
            if not uri:
                continue
            links.append(
                {
                    "type": "resource_link",
                    "uri": uri,
                    "name": item.get("title") or item.get("term") or uri,
                    "mimeType": "application/json",
                }
            )
        return links

    @staticmethod
    def _error_result(message: str) -> dict:
        return {"content": [{"type": "text", "text": message}], "isError": True}


__all__ = ["READ_INSTRUCTIONS", "WRITE_INSTRUCTIONS", "McpServer"]
