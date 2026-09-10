from django.apps import AppConfig


class McpConfig(AppConfig):
    """IP-014: Model Context Protocol server.

    Exposes the catalog to LLM agents as a set of read-only MCP tools over
    Streamable HTTP at `/mcp/v1`. The app owns the JSON-RPC transport and the
    tool registry only — every tool answers by composing the existing
    `apps.api` filters, so catalog access control has exactly one
    implementation.
    """

    name = "apps.mcp"
    verbose_name = "Model Context Protocol server"
