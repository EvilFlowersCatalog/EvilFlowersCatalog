"""JSON-RPC 2.0 framing and MCP protocol-version negotiation.

The Model Context Protocol rides on JSON-RPC 2.0. This module owns the wire
envelope and nothing else — it knows nothing about catalogs, entries or Django,
which keeps the transport testable in isolation.
"""

from typing import Any, Optional

# Protocol revisions this server speaks, newest first. `initialize` echoes the
# revision the client asked for when we support it; otherwise it answers with
# the newest we know and lets the client decide whether to continue.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# Streamable HTTP says a request without an `MCP-Protocol-Version` header is to
# be treated as 2025-03-26 — the last revision that predates the header.
ASSUMED_PROTOCOL_VERSION = "2025-03-26"

PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"

#: `structuredContent` on a tool result and the `resource_link` content block
#: both arrived in this revision. Revision identifiers are ISO dates, so a
#: string comparison orders them correctly.
STRUCTURED_OUTPUT_SINCE = "2025-06-18"


def supports_structured_output(version: str) -> bool:
    """Whether this revision understands `structuredContent` and `resource_link`.

    Emitting them to an older client is *probably* harmless — JSON consumers
    ignore unknown keys — but a strict client validating content-block types
    against its own revision would be entitled to reject the result. Gating
    costs one comparison.
    """
    return bool(version) and version >= STRUCTURED_OUTPUT_SINCE


JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """A protocol-level failure — malformed envelope, unknown method, bad params.

    Failures *inside* a tool are not these: they come back as a normal
    `tools/call` result carrying `isError: true`, so the model can read what
    went wrong and retry. Reserve `JsonRpcError` for the cases where the
    request itself never made sense.
    """

    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_error(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


def negotiate_protocol_version(requested: Any) -> str:
    """Pick the revision to answer `initialize` with."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def success(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def failure(request_id: Any, error: JsonRpcError) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error.as_error()}


__all__ = [
    "ASSUMED_PROTOCOL_VERSION",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "JsonRpcError",
    "LATEST_PROTOCOL_VERSION",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION_HEADER",
    "STRUCTURED_OUTPUT_SINCE",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "failure",
    "negotiate_protocol_version",
    "success",
    "supports_structured_output",
]
