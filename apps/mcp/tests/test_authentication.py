"""IP-014: how the endpoint authenticates, and everything it refuses.

The point of reusing `SecuredView` is that an MCP client presents exactly what a
REST client does. These go through the URLconf and the real `BearerBackend` and
`BasicBackend`, so that claim is verified end to end rather than assumed — and so
are the three restrictions layered on top: no tokens in the query string, a
configurable set of accepted schemes, and optionally no anonymous sessions at all.
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


def basic(username: str, password: str) -> str:
    import base64

    return f"Basic {base64.b64encode(f'{username}:{password}'.encode()).decode()}"


class BasicAuthenticationTests(AuthenticationTestCase):
    """Username/password, through the same `AuthSource` chain the REST API uses.

    Many MCP clients can only attach a username and a password, and a librarian
    curating the catalog through an agent should not have to mint an API key
    first. Basic therefore ships enabled — and an operator who would rather
    agents never hold a reusable password can narrow the endpoint back to Bearer.
    """

    def test_a_username_and_password_authenticate_the_session(self):
        headers = {"Authorization": basic("agent", "correct-horse-battery-staple")}
        body = json.loads(self.call("whoami", headers=headers).content)

        self.assertEqual(body["result"]["structuredContent"]["username"], "agent")

    def test_it_unlocks_exactly_what_the_api_key_unlocks(self):
        headers = {"Authorization": basic("agent", "correct-horse-battery-staple")}
        body = json.loads(self.call("search_entries", headers=headers).content)

        self.assertEqual([item["title"] for item in body["result"]["structuredContent"]["items"]], ["Members Only"])

    def test_a_wrong_password_is_refused_with_a_challenge(self):
        response = self.call("whoami", headers={"Authorization": basic("agent", "wrong")})

        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_a_malformed_credential_is_refused(self):
        response = self.call("whoami", headers={"Authorization": "Basic not-base64!!"})
        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)

    def test_management_tools_work_over_basic(self):
        """The write surface must not quietly depend on which scheme was used."""
        self.user.catalogs.through.objects.filter(user=self.user, catalog=self.catalog).update(mode="manage")
        headers = {"Authorization": basic("agent", "correct-horse-battery-staple")}

        body = json.loads(
            self.call(
                "create_category",
                {"catalog_id": str(self.catalog.pk), "term": "over-basic"},
                headers=headers,
            ).content
        )

        self.assertFalse(body["result"]["isError"])
        self.assertEqual(body["result"]["structuredContent"]["category"]["term"], "over-basic")


class SchemeRestrictionTests(AuthenticationTestCase):
    @override_settings(EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS=["Bearer"])
    def test_basic_can_be_switched_off(self):
        response = self.call("whoami", headers={"Authorization": basic("agent", "correct-horse-battery-staple")})

        self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)
        self.assertIn("Bearer", json.loads(response.content)["detail"])

    @override_settings(EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS=["Bearer"])
    def test_a_refusal_never_suggests_a_scheme_the_endpoint_rejects(self):
        # The hint a model reads has to track the configuration, or it will
        # keep retrying with a credential this deployment will never accept.
        body = json.loads(self.call("get_my_shelf").content)

        self.assertNotIn("Basic", body["result"]["content"][0]["text"])

    def test_a_refusal_offers_both_schemes_by_default(self):
        body = json.loads(self.call("get_my_shelf").content)
        message = body["result"]["content"][0]["text"]

        self.assertIn("Bearer", message)
        self.assertIn("Basic", message)

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

    def test_it_declares_every_accepted_scheme(self):
        payload = json.loads(self.client.get("/.well-known/oauth-protected-resource/mcp/v1").content)
        self.assertEqual(payload["authentication_schemes_supported"], ["Bearer", "Basic"])

        with override_settings(EVILFLOWERS_MCP_AUTHENTICATION_SCHEMAS=["Bearer"]):
            payload = json.loads(self.client.get("/.well-known/oauth-protected-resource/mcp/v1").content)
            self.assertEqual(payload["authentication_schemes_supported"], ["Bearer"])

    def test_a_401_challenges_with_every_accepted_scheme(self):
        response = self.call("whoami", token="not-a-jwt")
        header = response.headers["WWW-Authenticate"]

        self.assertIn("Bearer realm=", header)
        self.assertIn("Basic realm=", header)
        # Bearer first: a client that takes the first challenge it recognises
        # should land on the revocable credential, not on the password.
        self.assertLess(header.index("Bearer"), header.index("Basic"))

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
