"""The Streamable HTTP endpoint: `POST /mcp/v1`.

The MCP Streamable HTTP transport allows a server to answer a POST with either
an SSE stream or a single `application/json` body. This server always answers
with JSON: it never initiates a message, so it has nothing to stream. That
choice keeps the endpoint a plain synchronous Django view, deployable behind the
same WSGI/gunicorn workers as the rest of the project — no ASGI, no long-lived
connections, no extra dependency.

Consequently `GET` (the SSE-stream opener) and `DELETE` (session teardown) are
answered with 405, which the transport explicitly permits for a server that
offers neither.

Authentication is `SecuredView`'s — the same credentials as the REST API — with
three deliberate restrictions layered on top, all in `_authenticate` below:
tokens may not arrive in the query string, the accepted schemes are configurable
(Bearer API keys and Basic username/password, both on by default), and anonymous
access can be switched off entirely.
"""

import json
from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils.translation import gettext as _

from apps.core.errors import DetailType, ProblemDetailException
from apps.core.views import SecuredView
from apps.mcp.metadata import challenge, credential_hint, schemes
from apps.mcp.protocol import (
    ASSUMED_PROTOCOL_VERSION,
    INVALID_REQUEST,
    PARSE_ERROR,
    PROTOCOL_VERSION_HEADER,
    SUPPORTED_PROTOCOL_VERSIONS,
    JsonRpcError,
    failure,
)
from apps.mcp.server import McpServer
from apps.mcp.tools import registry

server = McpServer(registry)


class McpEndpoint(SecuredView):
    """MCP over Streamable HTTP, authenticated with the catalog's own credentials.

    An MCP client presents exactly what a REST client does: either
    `Authorization: Bearer <api key JWT>` or `Authorization: Basic <base64
    username:password>`, the latter resolving through whichever `AuthSource`
    (database or LDAP) recognises the user. No header at all is a valid
    anonymous session limited to public catalogs — that is what makes an open
    OPDS catalog usable by an agent without provisioning a credential first, and
    it can be turned off with `EVILFLOWERS_MCP_REQUIRE_AUTHENTICATION`.
    """

    http_method_names = ["post", "get", "delete", "options"]

    # -- authentication -------------------------------------------------

    def _authenticate(self, request):
        self._reject_query_string_token(request)
        self._assert_supported_scheme(request)

        try:
            user = super()._authenticate(request)
        except ProblemDetailException as exc:
            raise self._as_challenge(request, exc) from exc

        if settings.EVILFLOWERS_MCP_REQUIRE_AUTHENTICATION and not user.is_authenticated:
            raise self._unauthorized(
                request,
                _("This MCP endpoint requires credentials. Send %(hint)s.") % {"hint": credential_hint()},
            )

        return user

    @staticmethod
    def _reject_query_string_token(request) -> None:
        """`SecuredView` accepts `?access_token=`; here it is refused.

        A token in a URL leaks into access logs, proxy logs and browser history,
        and MCP clients have no reason to use one — they all send headers. The
        parent's behaviour stays intact for the REST and OPDS surfaces that
        depend on it (notification links, feed readers).
        """
        if "access_token" in request.GET:
            raise ProblemDetailException(
                _("Credentials must not be passed in the query string"),
                status=HTTPStatus.BAD_REQUEST,
                detail=_("Send %(hint)s as a header instead.") % {"hint": credential_hint()},
            )

    def _assert_supported_scheme(self, request) -> None:
        header = request.headers.get("Authorization", "")
        if not header:
            return

        scheme = header.split(" ")[0]
        allowed = schemes()
        if scheme in allowed:
            return

        # Both Bearer and Basic ship enabled, but an operator can narrow this to
        # Bearer alone — Basic means an agent's config file holds a reusable
        # password, and with an LDAP auth source that is the directory password.
        raise self._unauthorized(
            request,
            _("Unsupported authentication scheme '%(scheme)s'. This endpoint accepts: %(allowed)s.")
            % {"scheme": scheme, "allowed": ", ".join(allowed) or _("none")},
        )

    @staticmethod
    def _unauthorized(request, detail: str) -> ProblemDetailException:
        return ProblemDetailException(
            _("Unauthorized"),
            status=HTTPStatus.UNAUTHORIZED,
            detail=detail,
            extra_headers=(("WWW-Authenticate", challenge(request)),),
        )

    def _as_challenge(self, request, exc: ProblemDetailException) -> ProblemDetailException:
        """Re-raise an auth failure carrying the discovery pointer clients look for.

        `BearerBackend` raises a bare 401; MCP clients read `WWW-Authenticate`
        (and its `resource_metadata` parameter) to work out how to authenticate.
        """
        if exc.status != HTTPStatus.UNAUTHORIZED:
            return exc
        return self._unauthorized(request, exc.detail or str(exc))

    # -- transport ------------------------------------------------------

    def post(self, request):
        self._assert_origin(request)

        content_type = (request.content_type or "").lower()
        if content_type and not content_type.startswith("application/json"):
            return self._transport_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                INVALID_REQUEST,
                _("Content-Type must be application/json."),
            )

        if len(request.body) > settings.EVILFLOWERS_MCP_MAX_REQUEST_BYTES:
            return self._transport_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                INVALID_REQUEST,
                _("Request body exceeds %(limit)d bytes.") % {"limit": settings.EVILFLOWERS_MCP_MAX_REQUEST_BYTES},
            )

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._transport_error(HTTPStatus.BAD_REQUEST, PARSE_ERROR, f"Malformed JSON: {exc}")

        is_batch = isinstance(payload, list)
        messages = payload if is_batch else [payload]
        if is_batch and not messages:
            return self._transport_error(HTTPStatus.BAD_REQUEST, INVALID_REQUEST, _("Empty JSON-RPC batch."))

        version = self._negotiate_transport_version(request, messages)

        responses = [
            response for response in (server.handle(request, message, version) for message in messages) if response
        ]

        if not responses:
            # Notifications only — JSON-RPC forbids a response body.
            return self._with_version(HttpResponse(status=HTTPStatus.ACCEPTED), version)

        body = responses if is_batch else responses[0]
        response = JsonResponse(body, safe=False, json_dumps_params={"ensure_ascii": False})
        return self._with_version(response, self._negotiated_version(responses, version))

    def get(self, request):
        return self._method_not_allowed(
            _("This MCP endpoint does not open SSE streams; POST JSON-RPC messages to it instead.")
        )

    def delete(self, request):
        return self._method_not_allowed(_("This MCP endpoint is stateless and issues no session to terminate."))

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _assert_origin(request) -> None:
        """Guard against DNS-rebinding when the endpoint is reachable from a browser.

        Only enforced when `EVILFLOWERS_MCP_ALLOWED_ORIGINS` is configured. It
        has to be opt-in: the project ships `CORS_ALLOW_ALL_ORIGINS = True`, and
        a non-browser MCP client (the common case) sends no `Origin` at all.
        """
        allowed = settings.EVILFLOWERS_MCP_ALLOWED_ORIGINS
        if not allowed:
            return

        origin = request.headers.get("Origin")
        if origin is None or origin in allowed:
            return

        raise ProblemDetailException(
            _("Origin not allowed"),
            status=HTTPStatus.FORBIDDEN,
            detail_type=DetailType.FORBIDDEN,
            detail=_("Origin '%(origin)s' may not reach the MCP endpoint.") % {"origin": origin},
        )

    @staticmethod
    def _negotiate_transport_version(request, messages) -> str:
        """Validate the `MCP-Protocol-Version` header a client sends after initialize.

        The header is absent on the very first request and, per the transport
        spec, absence means 2025-03-26. An `initialize` call is exempt from the
        check entirely — that call *is* the negotiation, and rejecting a client
        for advertising a revision we have not heard of would stop it from ever
        discovering which ones we do speak.
        """
        version = request.headers.get(PROTOCOL_VERSION_HEADER)
        if version is None:
            return ASSUMED_PROTOCOL_VERSION

        if version in SUPPORTED_PROTOCOL_VERSIONS:
            return version

        if any(isinstance(message, dict) and message.get("method") == "initialize" for message in messages):
            return ASSUMED_PROTOCOL_VERSION

        raise ProblemDetailException(
            _("Unsupported MCP protocol version"),
            status=HTTPStatus.BAD_REQUEST,
            detail=_("'%(version)s' is not supported. This server speaks: %(supported)s.")
            % {"version": version, "supported": ", ".join(SUPPORTED_PROTOCOL_VERSIONS)},
        )

    @staticmethod
    def _negotiated_version(responses, fallback: str) -> str:
        """On the `initialize` turn, echo back the revision we just agreed on.

        Before initialize there is nothing to echo but the transport default,
        which would tell the client 2025-03-26 in the very response that
        negotiated something newer.
        """
        for response in responses:
            agreed = (response.get("result") or {}).get("protocolVersion")
            if agreed:
                return agreed
        return fallback

    @staticmethod
    def _with_version(response: HttpResponse, version: str) -> HttpResponse:
        response[PROTOCOL_VERSION_HEADER] = version
        return response

    @staticmethod
    def _transport_error(status: HTTPStatus, code: int, message: str) -> JsonResponse:
        """A failure that happened before any message could be identified.

        There is no request `id` to correlate against, so `id` is null — which
        JSON-RPC prescribes exactly for parse and invalid-request errors.
        """
        return JsonResponse(failure(None, JsonRpcError(code, message)), status=status)

    @staticmethod
    def _method_not_allowed(message: str) -> JsonResponse:
        response = JsonResponse(
            failure(None, JsonRpcError(INVALID_REQUEST, message)),
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )
        response["Allow"] = "POST"
        return response


__all__ = ["McpEndpoint"]
