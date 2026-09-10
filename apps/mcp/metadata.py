"""OAuth 2.0 Protected Resource Metadata (RFC 9728) for the MCP endpoint.

The MCP authorization spec treats an HTTP MCP server as an OAuth Resource
Server and expects a 401 to point at a metadata document describing how to
authenticate. This catalog is not an OAuth deployment — tokens are API keys
issued by the catalog itself — but the discovery document is still the right
thing to serve: it tells a client the resource identifier, that bearer tokens go
in the `Authorization` header, and where the human-readable instructions are.

What it deliberately does **not** claim is an `authorization_servers` list.
That field is optional in RFC 9728, and pointing at an authorization server
this deployment does not run would send clients into a discovery flow that
cannot complete — worse than saying nothing. A client that requires full OAuth
will therefore not connect, which is the honest outcome; see IP-014 Q3.
"""

from http import HTTPStatus

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.views import View


class ProtectedResourceMetadata(View):
    """`GET /.well-known/oauth-protected-resource[/mcp/v1]`.

    Unauthenticated by design — discovery metadata is public, and a client that
    cannot read it has no way to learn how to authenticate.
    """

    http_method_names = ["get", "options"]

    def get(self, request, *args, **kwargs):
        resource = request.build_absolute_uri(reverse("mcp:endpoint"))

        payload = {
            "resource": resource,
            "bearer_methods_supported": ["header"],
            "resource_name": settings.INSTANCE_NAME,
            "resource_documentation": "https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/wiki/Model-Context-Protocol",
            "scopes_supported": _scopes(),
        }

        response = JsonResponse(payload, status=HTTPStatus.OK)
        # Public, cacheable, and it changes only when the deployment does.
        response["Cache-Control"] = "public, max-age=3600"
        return response


def _scopes() -> list:
    """The access levels this deployment actually offers.

    Not OAuth scopes a client can request — the catalog has no consent screen —
    but an accurate description of what a credential can reach here, which is
    what a human reading the discovery document wants to know.
    """
    scopes = ["catalog:read"]
    if settings.EVILFLOWERS_MCP_ALLOW_WRITE:
        scopes.append("catalog:manage")
    return scopes


def challenge(request) -> str:
    """The `WWW-Authenticate` value for a 401 from the MCP endpoint."""
    from django.utils.text import slugify

    metadata_url = request.build_absolute_uri(reverse("mcp-protected-resource-metadata"))
    return f'Bearer realm="{slugify(settings.INSTANCE_NAME)}", resource_metadata="{metadata_url}"'


__all__ = ["ProtectedResourceMetadata", "challenge"]
