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
from opentelemetry.trace import SpanKind, StatusCode

from apps.core.errors import ProblemDetailException
from apps.core.fields.localized import localize
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
from evil_flowers_catalog import telemetry

logger = logging.getLogger("apps.mcp.server")
audit = logging.getLogger("apps.mcp.audit")

# Semantic conventions for MCP (`mcp.*`, `gen_ai.tool.name`, `error.type`).
_tracer = telemetry.get_tracer("apps.mcp")
_operation_duration = telemetry.get_meter("apps.mcp").create_histogram(
    "mcp.server.operation.duration",
    unit="s",
    description="Duration of one MCP request handled by the server.",
    explicit_bucket_boundaries_advisory=[0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300],
)

KNOWN_METHODS = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
        "completion/complete",
        "notifications/initialized",
        "notifications/cancelled",
    }
)

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
    "authenticated user's own library and require credentials. `get_user` and `list_api_keys` "
    "describe the caller's own account and API keys; administrators see everyone's through "
    "`list_users` and `get_user`."
)

WRITE_INSTRUCTIONS = (
    "\n\nThis deployment also lets you curate the library: catalogs, feeds and categories can be "
    "created, updated and deleted, publications can be filed under categories "
    "(`classify_entries`) and moved in and out of feeds (`add_entries_to_feed`, "
    "`remove_entries_from_feed`).\n\n"
    "Publications themselves are added with `create_entry`, corrected with `update_entry` and "
    "removed with `delete_entry` (files included — run it with `dry_run: true` first and show "
    "the user the report). A "
    "file never travels through a tool call: `create_upload_link` returns a signed single-use URL "
    "and the file is POSTed there as multipart `content`. Set `lcp_enabled` and `lcp_copies` for "
    "DRM-protected titles — encryption is queued when the file arrives; `ip_block`, `allow_print`, "
    "`allow_share` and `allow_annotations` set who may read it and how. The catalog refuses a "
    "second publication with the same title, ISBN or DOI; search before importing.\n\n"
    "Every curation tool needs `manage` access on the catalog in question. Start with "
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
    "publication and file inside it — as something to propose, never to volunteer.\n\n"
    "Administrators also get operations tools for Readium LCP encryption: "
    "`list_encryption_jobs` finds jobs stuck in `pending` or `failed` (a publication cannot be "
    "lent until its job is `registered`), and `requeue_encryption` sends them to the worker again. "
    "Re-queuing does not fix whatever lost the job — say so if the cause looks like deployment. "
    "`reencrypt_publication` re-runs an already `registered` job, e.g. after a worker upgrade; run "
    "it with `dry_run: true` first, and never pass `force` (which breaks active loans) without "
    "the user's confirmation.\n\n"
    "Superusers also shape the notification e-mails: `get_notification_branding` shows the effective "
    "branding, the logo images and template overrides on the deployment's volume; "
    "`update_notification_branding` changes name, logo, colours and footer for every e-mail — check "
    "the result with `preview_notification` and `send_test_notification` (your own inbox only). "
    "`sync_ldap_profiles` imports e-mail addresses of LDAP users who have not logged in yet; run it "
    "with `dry_run: true` first.\n\n"
    "Accounts and credentials: `create_api_key` mints a Bearer key for the caller and returns its "
    "token exactly once — give it to the user straight away, it cannot be retrieved later; "
    "`revoke_api_key` disables one. Administrators can also `create_user`, `update_user` and "
    "`delete_user`, and a superuser can mint a key for another account (e.g. an API-key-only "
    "service account created without a password). Nobody can change their own role, active flag "
    "or password here, and only a superuser can touch an administrator's account. Prefer "
    "deactivating a user over `delete_user`, which also destroys their loans and annotations."
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

        Every message gets one INTERNAL span, `"{mcp.method.name} {gen_ai.tool.name}"`,
        and one `mcp.server.operation.duration` point (IP-019). Method and tool
        names come from the client, so anything we do not know is `_OTHER`.
        """
        method, tool = self._telemetry_names(message)
        attributes = {"mcp.method.name": method}
        if tool:
            attributes["gen_ai.tool.name"] = tool
        stopwatch = telemetry.Stopwatch()

        with _tracer.start_as_current_span(
            f"{method} {tool}" if tool else method, kind=SpanKind.INTERNAL, attributes=attributes
        ) as span:
            response = self._handle(request, message, protocol_version)

            error_type = self._error_type(response)
            if error_type:
                attributes["error.type"] = error_type
                span.set_attribute("error.type", error_type)
                span.set_status(StatusCode.ERROR)

        _operation_duration.record(stopwatch.elapsed, attributes)
        return response

    def _telemetry_names(self, message):
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return "_OTHER", None
        method = message["method"] if message["method"] in KNOWN_METHODS else "_OTHER"
        if method != "tools/call":
            return method, None
        name = (message.get("params") or {}).get("name") if isinstance(message.get("params"), dict) else None
        return method, (name if isinstance(name, str) and self._registry.get(name) else "_OTHER")

    @staticmethod
    def _error_type(response) -> Optional[str]:
        """JSON-RPC error code, or `tool_error` for a tool result with `isError`."""
        if not isinstance(response, dict):
            return None
        if isinstance(response.get("error"), dict):
            return str(response["error"].get("code", "_OTHER"))
        result = response.get("result")
        if isinstance(result, dict) and result.get("isError"):
            return "tool_error"
        return None

    def _handle(self, request, message, protocol_version: str = ""):
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
                    _("`%(tool)s` changes data and requires an authenticated session. " "Connect with %(hint)s.")
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
                    # `name` must be a string. Feed titles and category labels are
                    # localized mappings ({"sk": ..., "en": ...}); passing one through
                    # made clients reject the whole result — after the write had
                    # already been committed.
                    "name": localize(item.get("title")) or localize(item.get("term")) or uri,
                    "mimeType": "application/json",
                }
            )
        return links

    @staticmethod
    def _error_result(message: str) -> dict:
        return {"content": [{"type": "text", "text": message}], "isError": True}


__all__ = ["READ_INSTRUCTIONS", "WRITE_INSTRUCTIONS", "McpServer"]
