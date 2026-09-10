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
            "resource_name": settings.INSTANCE_NAME,
            "resource_documentation": "https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/wiki/Model-Context-Protocol",
            "scopes_supported": _scopes(),
            # Not an RFC 9728 member. RFC 9728 describes bearer tokens only, so
            # a deployment that also accepts Basic has no standard field to say
            # so. Extra members are permitted and a client that does not know
            # this one still learns everything the standard fields carry.
            "authentication_schemes_supported": list(schemes()),
        }

        if "Bearer" in schemes():
            payload["bearer_methods_supported"] = ["header"]

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


def schemes() -> tuple:
    """The authentication schemes this deployment accepts on `/mcp/v1`.

    Ordered Bearer-first regardless of how the operator wrote the setting: a
    client that picks the first challenge it recognises should land on the
    revocable credential rather than on the password.
    """
    configured = list(settings.EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS)
    return tuple(sorted(configured, key=lambda scheme: (scheme != "Bearer", scheme)))


def credential_hint() -> str:
    """How to authenticate, phrased for a model reading a refusal message.

    Every "you need credentials" message in this app routes through here, so a
    deployment that turns Basic off never tells an agent to try it — and one
    that turns Bearer off never tells it to mint an API key it cannot use.
    """
    from django.utils.translation import gettext as _

    forms = {
        "Bearer": _("`Authorization: Bearer <api key>`"),
        "Basic": _("`Authorization: Basic <base64 of username:password>`"),
    }
    offered = [forms[scheme] for scheme in schemes() if scheme in forms]

    if not offered:
        return _("no authentication scheme is enabled on this endpoint")
    if len(offered) == 1:
        return offered[0]
    return _("%(first)s or %(second)s") % {"first": offered[0], "second": offered[1]}


def challenge(request) -> str:
    """The `WWW-Authenticate` value for a 401 from the MCP endpoint.

    One comma-separated challenge per accepted scheme, which is what RFC 9110
    prescribes for a resource offering a choice. Only the Bearer challenge
    carries `resource_metadata` — that parameter is defined by RFC 9728 for
    bearer tokens, and MCP clients look for it there.
    """
    from django.utils.text import slugify

    realm = slugify(settings.INSTANCE_NAME)
    metadata_url = request.build_absolute_uri(reverse("mcp-protected-resource-metadata"))

    challenges = []
    for scheme in schemes():
        if scheme == "Bearer":
            challenges.append(f'Bearer realm="{realm}", resource_metadata="{metadata_url}"')
        else:
            challenges.append(f'{scheme} realm="{realm}"')

    # With every scheme disabled there is still a 401 to answer, and a response
    # without `WWW-Authenticate` is malformed. Name the one we would prefer.
    return ", ".join(challenges) or f'Bearer realm="{realm}"'


__all__ = ["ProtectedResourceMetadata", "challenge", "credential_hint", "schemes"]
