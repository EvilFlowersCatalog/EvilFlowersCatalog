from django.urls import path

from apps.mcp.views import McpEndpoint

# A single endpoint is the whole surface: the MCP Streamable HTTP transport
# multiplexes every method over one URL.
urlpatterns = [
    path("", McpEndpoint.as_view(), name="endpoint"),
]
