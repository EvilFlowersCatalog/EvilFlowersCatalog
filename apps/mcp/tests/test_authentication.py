"""IP-014: how the endpoint authenticates, and everything it refuses.

The point of reusing `SecuredView` is that an MCP client presents exactly what a
REST client does. These go through the URLconf and the real `BearerBackend`, so
that claim is verified end to end rather than assumed — and so are the three
restrictions layered on top: no tokens in the query string, Bearer only, and
optionally no anonymous sessions at all.
"""

import json
from http import HTTPStatus

from django.test import TestCase, override_settings

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Entry, User
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION, PROTOCOL_VERSION_HEADER

URL = "/mcp/v1"


class AuthenticationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="agent", name="Agent", surname="Smith", auth_source=auth_source)
        cls.user.set_password("correct-horse-battery-staple")
        cls.user.save()

        cls.api_key = ApiKey.objects.create(user=cls.user, name="mcp-client")
        cls.token = JWTFactory(cls.user.pk).api_key(str(cls.api_key.pk))

        cls.catalog = Catalog.objects.create(creator=cls.user, title="Private", url_name="private")
        cls.user.catalogs.add(cls.catalog, through_defaults={"mode": "read"})
        Entry.objects.create(creator=cls.user, catalog=cls.catalog, title="Members Only")

    def post(self, payload, *, token=None, headers=None, url=URL):
        merged = {PROTOCOL_VERSION_HEADER: LATEST_PROTOCOL_VERSION}
        if token:
            merged["Authorization"] = f"Bearer {token}"
        merged.update(headers or {})
        return self.client.post(url, data=json.dumps(payload), content_type="application/json", headers=merged)

    def call(self, name, arguments=None, *, token=None, headers=None, url=URL):
        return self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            token=token,
            headers=headers,
            url=url,
        )


class ApiKeyTests(AuthenticationTestCase):
    def test_an_api_key_authenticates_the_session(self):
        body = json.loads(self.call("whoami", token=self.token).content)
        self.assertEqual(body["result"]["structuredContent"]["username"], "agent")

    def test_an_api_key_unlocks_its_users_catalogs(self):
        body = json.loads(self.call("search_entries", token=self.token).content)
        titles = [item["title"] for item in body["result"]["structuredContent"]["items"]]
        self.assertEqual(titles, ["Members Only"])

    def test_the_same_search_returns_nothing_anonymously(self):
        body = json.loads(self.call("search_entries").content)
        self.assertEqual(body["result"]["structuredContent"]["items"], [])

    def test_personal_tools_open_up_once_authenticated(self):
        body = json.loads(self.call("list_my_loans", token=self.token).content)
        self.assertFalse(body["result"]["isError"])

    def test_a_revoked_api_key_is_rejected_with_a_challenge(self):
        ApiKey.objects.filter(pk=self.api_key.pk).update(is_active=False)
        response = self.call("whoami", token=self.token)
        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)
        self.assertIn("Bearer", response.headers["WWW-Authenticate"])

    def test_a_garbage_token_is_rejected(self):
        self.assertEqual(self.call("whoami", token="not-a-jwt").status_code, HTTPStatus.UNAUTHORIZED)


class SchemeRestrictionTests(AuthenticationTestCase):
    def test_basic_authentication_is_refused_by_default(self):
        """An agent config holding a password is a worse credential than an API key.

        `SecuredView` accepts Basic for the REST and OPDS surfaces; this
        endpoint narrows it to Bearer via `EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS`.
        """
        import base64

        encoded = base64.b64encode(b"agent:correct-horse-battery-staple").decode()
        response = self.call("whoami", headers={"Authorization": f"Basic {encoded}"})

        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)
        self.assertIn("Bearer", json.loads(response.content)["detail"])

    @override_settings(EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS=["Bearer", "Basic"])
    def test_basic_can_be_re_enabled_by_configuration(self):
        import base64

        encoded = base64.b64encode(b"agent:correct-horse-battery-staple").decode()
        body = json.loads(self.call("whoami", headers={"Authorization": f"Basic {encoded}"}).content)

        self.assertEqual(body["result"]["structuredContent"]["username"], "agent")

    def test_an_unknown_scheme_is_rejected(self):
        response = self.call("whoami", headers={"Authorization": "Negotiate abcdef"})
        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)


class QueryStringTokenTests(AuthenticationTestCase):
    def test_a_token_in_the_query_string_is_refused(self):
        """`SecuredView` honours `?access_token=`; this endpoint must not.

        URLs reach access logs, proxy logs and history. Refusing loudly beats
        silently accepting a credential that has already been written down
        somewhere it should not be.
        """
        response = self.call("whoami", url=f"{URL}?access_token={self.token}")

        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertIn("query string", json.loads(response.content)["title"])

    def test_it_is_refused_even_when_the_token_is_worthless(self):
        # The refusal is about the channel, not the credential's validity.
        response = self.call("whoami", url=f"{URL}?access_token=nonsense")
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)


@override_settings(EVILFLOWERS_MCP_REQUIRE_AUTHENTICATION=True)
class RequiredAuthenticationTests(AuthenticationTestCase):
    def test_anonymous_sessions_are_refused_at_the_transport(self):
        response = self.call("search_entries")
        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)
        self.assertIn("Bearer", response.headers["WWW-Authenticate"])

    def test_even_initialize_is_refused(self):
        # Authentication is checked in `dispatch`, before any JSON-RPC method
        # runs — a closed deployment should not disclose its tool list either.
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)

    def test_a_credential_still_works(self):
        body = json.loads(self.call("whoami", token=self.token).content)
        self.assertTrue(body["result"]["structuredContent"]["authenticated"])


class ProtectedResourceMetadataTests(AuthenticationTestCase):
    """RFC 9728 discovery — how a client learns to authenticate."""

    def test_the_metadata_document_is_served_and_public(self):
        response = self.client.get("/.well-known/oauth-protected-resource/mcp/v1")
        self.assertEqual(response.status_code, HTTPStatus.OK)

        payload = json.loads(response.content)
        self.assertTrue(payload["resource"].endswith("/mcp/v1"))
        self.assertEqual(payload["bearer_methods_supported"], ["header"])

    def test_it_does_not_advertise_an_authorization_server_it_does_not_run(self):
        # Pointing at an AS this deployment has no way to serve would send
        # clients into a discovery flow that cannot complete. See IP-014 Q3.
        payload = json.loads(self.client.get("/.well-known/oauth-protected-resource/mcp/v1").content)
        self.assertNotIn("authorization_servers", payload)

    def test_the_bare_well_known_path_is_served_too(self):
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource").status_code, HTTPStatus.OK)

    def test_scopes_track_whether_writes_are_enabled(self):
        payload = json.loads(self.client.get("/.well-known/oauth-protected-resource/mcp/v1").content)
        self.assertIn("catalog:manage", payload["scopes_supported"])

        with override_settings(EVILFLOWERS_MCP_ALLOW_WRITE=False):
            payload = json.loads(self.client.get("/.well-known/oauth-protected-resource/mcp/v1").content)
            self.assertEqual(payload["scopes_supported"], ["catalog:read"])

    def test_a_401_points_at_the_metadata_document(self):
        response = self.call("whoami", token="not-a-jwt")
        self.assertIn("resource_metadata=", response.headers["WWW-Authenticate"])


class RequestLimitTests(AuthenticationTestCase):
    @override_settings(EVILFLOWERS_MCP_MAX_REQUEST_BYTES=200)
    def test_an_oversized_body_is_refused_before_parsing(self):
        response = self.call("search_entries", {"query": "x" * 5000}, token=self.token)
        self.assertEqual(response.status_code, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
