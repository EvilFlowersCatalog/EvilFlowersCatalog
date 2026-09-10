"""IP-014: resources, prompts, completions, and resource links.

The recurring risk across all three is the same one: a surface that is not a
tool can quietly skip the access control the tools apply. Each section below
checks that it does not.
"""

import json

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from apps.core.models import AuthSource, Catalog, Category, Entry, Feed, User, UserCatalog
from apps.mcp.protocol import ASSUMED_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry
from apps.mcp import uris


class SurfaceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)

        cls.member = cls._user(auth_source, "member")
        cls.outsider = cls._user(auth_source, "outsider")

        cls.public_catalog = Catalog.objects.create(
            creator=cls.member, title="Open Shelf", url_name="open", is_public=True
        )
        cls.private_catalog = Catalog.objects.create(creator=cls.member, title="Closed Shelf", url_name="closed")
        UserCatalog.objects.create(user=cls.member, catalog=cls.private_catalog, mode=UserCatalog.Mode.MANAGE)

        cls.public_entry = Entry.objects.create(creator=cls.member, catalog=cls.public_catalog, title="Public Book")
        cls.private_entry = Entry.objects.create(creator=cls.member, catalog=cls.private_catalog, title="Private Book")
        cls.private_feed = Feed.objects.create(
            creator=cls.member,
            catalog=cls.private_catalog,
            title="Secret Shelf",
            url_name="secret",
            kind=Feed.FeedKind.NAVIGATION,
            source=Feed.FeedSource.RELATION,
            content="x",
        )
        Category.objects.create(
            creator=cls.member, catalog=cls.public_catalog, term="mathematics", label="Mathematics"
        )
        Category.objects.create(creator=cls.member, catalog=cls.private_catalog, term="classified")

    @staticmethod
    def _user(auth_source, username: str) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=auth_source)
        user.set_unusable_password()
        user.save()
        return user

    def setUp(self):
        self.factory = RequestFactory()
        self.server = McpServer(registry)

    def rpc(self, method: str, params: dict, user, version: str = LATEST_PROTOCOL_VERSION):
        request = self.factory.post("/mcp/v1")
        request.user = user
        return self.server.handle(request, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, version)

    def ok(self, method: str, params: dict, user) -> dict:
        response = self.rpc(method, params, user)
        self.assertNotIn("error", response, response.get("error"))
        return response["result"]

    def failed(self, method: str, params: dict, user) -> str:
        response = self.rpc(method, params, user)
        self.assertIn("error", response)
        return response["error"]["message"]


class ResourceListingTests(SurfaceTestCase):
    def test_catalogs_are_listed_as_resources(self):
        result = self.ok("resources/list", {}, self.member)
        uri_set = {item["uri"] for item in result["resources"]}
        self.assertEqual(
            uri_set,
            {uris.catalog_uri(self.public_catalog.pk), uris.catalog_uri(self.private_catalog.pk)},
        )

    def test_listing_is_scoped_to_the_caller(self):
        result = self.ok("resources/list", {}, self.outsider)
        self.assertEqual([item["uri"] for item in result["resources"]], [uris.catalog_uri(self.public_catalog.pk)])

    def test_anonymous_sees_public_catalogs_only(self):
        result = self.ok("resources/list", {}, AnonymousUser())
        self.assertEqual([item["uri"] for item in result["resources"]], [uris.catalog_uri(self.public_catalog.pk)])

    def test_templates_cover_the_collections_too_large_to_enumerate(self):
        result = self.ok("resources/templates/list", {}, self.member)
        templates = {item["uriTemplate"] for item in result["resourceTemplates"]}
        self.assertEqual(
            templates,
            {"evilflowers://entry/{id}", "evilflowers://feed/{id}", "evilflowers://catalog/{id}"},
        )

    def test_a_bad_cursor_is_reported_not_ignored(self):
        self.assertIn("Invalid cursor", self.failed("resources/list", {"cursor": "banana"}, self.member))


class ResourceReadTests(SurfaceTestCase):
    def _read(self, uri, user):
        return json.loads(self.ok("resources/read", {"uri": uri}, user)["contents"][0]["text"])

    def test_reading_an_entry_returns_its_detail(self):
        payload = self._read(uris.entry_uri(self.public_entry.pk), self.member)
        self.assertEqual(payload["title"], "Public Book")

    def test_reading_a_catalog_includes_counts(self):
        payload = self._read(uris.catalog_uri(self.private_catalog.pk), self.member)
        self.assertEqual(payload["entry_count"], 1)
        self.assertEqual(payload["feed_count"], 1)

    def test_reading_a_feed_works(self):
        payload = self._read(uris.feed_uri(self.private_feed.pk), self.member)
        self.assertEqual(payload["title"], "Secret Shelf")

    def test_a_resource_cannot_reveal_what_a_tool_would_refuse(self):
        """The whole point of routing reads through the same filters."""
        for uri in (
            uris.entry_uri(self.private_entry.pk),
            uris.catalog_uri(self.private_catalog.pk),
            uris.feed_uri(self.private_feed.pk),
        ):
            with self.subTest(uri=uri):
                self.assertIn(
                    "does not exist or is not readable", self.failed("resources/read", {"uri": uri}, self.outsider)
                )

    def test_an_unknown_scheme_is_explained(self):
        message = self.failed("resources/read", {"uri": "https://example.com/book"}, self.member)
        self.assertIn("not a resource URI this server serves", message)

    def test_a_malformed_uuid_is_rejected(self):
        self.assertIn(
            "not a resource URI", self.failed("resources/read", {"uri": "evilflowers://entry/banana"}, self.member)
        )


class PromptTests(SurfaceTestCase):
    def test_prompts_are_listed_with_their_arguments(self):
        result = self.ok("prompts/list", {}, self.member)
        names = {prompt["name"] for prompt in result["prompts"]}
        self.assertEqual(names, {"reading_list", "availability_report", "organise_catalog", "catalog_overview"})

    def test_a_prompt_renders_a_user_message(self):
        result = self.ok("prompts/get", {"name": "reading_list", "arguments": {"topic": "graph theory"}}, self.member)
        text = result["messages"][0]["content"]["text"]
        self.assertIn("graph theory", text)
        self.assertEqual(result["messages"][0]["role"], "user")

    def test_a_missing_required_argument_is_reported(self):
        self.assertIn(
            "requires the argument 'topic'",
            self.failed("prompts/get", {"name": "reading_list", "arguments": {}}, self.member),
        )

    def test_the_curation_prompt_insists_on_approval_before_writing(self):
        result = self.ok("prompts/get", {"name": "organise_catalog", "arguments": {"catalog": "STU"}}, self.member)
        text = result["messages"][0]["content"]["text"]
        self.assertIn("wait for my approval", text)

    def test_an_unknown_prompt_lists_the_real_ones(self):
        message = self.failed("prompts/get", {"name": "nope"}, self.member)
        self.assertIn("reading_list", message)


class CompletionTests(SurfaceTestCase):
    def _complete(self, name, value, user):
        return self.ok(
            "completion/complete",
            {"ref": {"type": "ref/tool", "name": "search_entries"}, "argument": {"name": name, "value": value}},
            user,
        )["completion"]["values"]

    def test_category_terms_complete_from_live_data(self):
        self.assertEqual(self._complete("category_term", "math", self.member), ["mathematics"])

    def test_completion_is_scoped_to_the_caller(self):
        """A completion must not leak a term from a catalog the caller cannot read."""
        self.assertIn("classified", self._complete("category_term", "class", self.member))
        self.assertEqual(self._complete("category_term", "class", self.outsider), [])

    def test_catalog_ids_complete(self):
        values = self._complete("catalog_id", "", self.member)
        self.assertIn(str(self.private_catalog.pk), values)
        self.assertNotIn(str(self.private_catalog.pk), self._complete("catalog_id", "", self.outsider))

    def test_an_argument_with_no_bounded_domain_completes_empty(self):
        self.assertEqual(self._complete("query", "anything", self.member), [])

    def test_an_unknown_argument_is_not_an_error(self):
        self.assertEqual(self._complete("nonsense", "x", self.member), [])


class ResourceLinkTests(SurfaceTestCase):
    def _call(self, name, arguments, user, version=LATEST_PROTOCOL_VERSION):
        request = self.factory.post("/mcp/v1")
        request.user = user
        return self.server.handle(
            request,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            version,
        )["result"]

    def test_search_results_carry_resource_links(self):
        result = self._call("search_entries", {}, self.member)
        links = [block for block in result["content"] if block["type"] == "resource_link"]

        self.assertTrue(links)
        self.assertTrue(all(link["uri"].startswith("evilflowers://entry/") for link in links))
        self.assertIn("Public Book", {link["name"] for link in links})

    def test_a_detail_result_carries_one_link(self):
        result = self._call("get_entry", {"entry_id": str(self.public_entry.pk)}, self.member)
        links = [block for block in result["content"] if block["type"] == "resource_link"]
        self.assertEqual([link["uri"] for link in links], [uris.entry_uri(self.public_entry.pk)])

    def test_older_clients_get_neither_links_nor_structured_content(self):
        """`resource_link` and `structuredContent` both postdate 2025-03-26.

        A strict older client validating content-block types would be entitled
        to reject an unknown one, so they are gated on the negotiated revision.
        """
        result = self._call("search_entries", {}, self.member, version=ASSUMED_PROTOCOL_VERSION)

        self.assertNotIn("structuredContent", result)
        self.assertEqual({block["type"] for block in result["content"]}, {"text"})
