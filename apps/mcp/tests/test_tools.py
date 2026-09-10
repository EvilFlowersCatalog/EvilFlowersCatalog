"""IP-014: the tools answer correctly, and never past the caller's access boundary.

The access-control cases are the point of this module. Every tool composes an
`apps.api` filter precisely so MCP inherits REST's catalog scoping; these tests
are what stops that from silently regressing into a second, laxer
implementation.
"""

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from apps.core.models import AuthSource, Author, Catalog, Category, Entry, EntryAuthor, ShelfRecord, User
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry


class ToolTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)

        cls.owner = cls._user("owner")
        cls.outsider = cls._user("outsider")

        cls.public_catalog = Catalog.objects.create(
            creator=cls.owner, title="Open Shelf", url_name="open", is_public=True
        )
        cls.private_catalog = Catalog.objects.create(creator=cls.owner, title="Closed Shelf", url_name="closed")
        cls.owner.catalogs.add(cls.private_catalog, through_defaults={"mode": "manage"})

        cls.author = Author.objects.create(catalog=cls.public_catalog, name="Ada", surname="Lovelace")
        cls.category = Category.objects.create(
            creator=cls.owner, catalog=cls.public_catalog, term="analytical-engine", label="Analytical Engine"
        )

        cls.public_entry = cls._entry(cls.public_catalog, "Notes on the Analytical Engine", publisher="Taylor")
        EntryAuthor.objects.create(entry=cls.public_entry, author=cls.author, position=0)
        cls.public_entry.categories.add(cls.category)

        cls.private_entry = cls._entry(cls.private_catalog, "Confidential Ledger")

    @classmethod
    def _user(cls, username: str) -> User:
        user = User(username=username, name=username.title(), surname="Tester", auth_source=cls.auth_source)
        user.set_unusable_password()
        user.save()
        return user

    @classmethod
    def _entry(cls, catalog: Catalog, title: str, **kwargs) -> Entry:
        return Entry.objects.create(creator=cls.owner, catalog=catalog, title=title, **kwargs)

    def setUp(self):
        self.factory = RequestFactory()
        self.server = McpServer(registry)

    def call(self, name: str, arguments: dict, user):
        request = self.factory.post("/mcp/v1")
        request.user = user
        response = self.server.handle(
            request,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            LATEST_PROTOCOL_VERSION,
        )
        return response["result"]

    def ok(self, name: str, arguments: dict, user) -> dict:
        result = self.call(name, arguments, user)
        self.assertFalse(result.get("isError"), result["content"][0]["text"] if result.get("isError") else "")
        return result["structuredContent"]

    @staticmethod
    def titles(payload: dict) -> list:
        return [item["title"] for item in payload["items"]]


class SearchTests(ToolTestCase):
    def test_free_text_search_matches_the_title(self):
        payload = self.ok("search_entries", {"query": "Analytical"}, self.owner)
        self.assertIn("Notes on the Analytical Engine", self.titles(payload))

    def test_search_projects_authors_and_categories(self):
        payload = self.ok("search_entries", {"query": "Analytical"}, self.owner)
        hit = payload["items"][0]
        self.assertEqual(hit["authors"], ["Ada Lovelace"])
        self.assertEqual(hit["categories"], ["analytical-engine"])

    def test_author_filter_by_id(self):
        payload = self.ok("search_entries", {"author_ids": [str(self.author.pk)]}, self.owner)
        self.assertEqual(self.titles(payload), ["Notes on the Analytical Engine"])

    def test_category_filter_by_id(self):
        payload = self.ok("search_entries", {"category_ids": [str(self.category.pk)]}, self.owner)
        self.assertEqual(self.titles(payload), ["Notes on the Analytical Engine"])

    def test_catalog_filter(self):
        payload = self.ok("search_entries", {"catalog_id": str(self.private_catalog.pk)}, self.owner)
        self.assertEqual(self.titles(payload), ["Confidential Ledger"])

    def test_explicit_ordering_is_honoured(self):
        payload = self.ok("search_entries", {"order_by": "title"}, self.owner)
        self.assertEqual(self.titles(payload), sorted(self.titles(payload)))

    def test_paging_past_the_end_clamps_instead_of_failing(self):
        payload = self.ok("search_entries", {"page": 500, "limit": 1}, self.owner)
        self.assertEqual(payload["metadata"]["page"], payload["metadata"]["pages"])
        self.assertFalse(payload["metadata"]["has_next_page"])

    def test_metadata_reports_the_total(self):
        payload = self.ok("search_entries", {"limit": 1}, self.owner)
        self.assertEqual(payload["metadata"]["total"], 2)
        self.assertTrue(payload["metadata"]["has_next_page"])


class AccessControlTests(ToolTestCase):
    def test_anonymous_sees_only_public_catalogs(self):
        payload = self.ok("search_entries", {}, AnonymousUser())
        self.assertEqual(self.titles(payload), ["Notes on the Analytical Engine"])

    def test_a_user_without_a_grant_sees_only_public_catalogs(self):
        payload = self.ok("search_entries", {}, self.outsider)
        self.assertEqual(self.titles(payload), ["Notes on the Analytical Engine"])

    def test_a_granted_user_sees_the_private_catalog(self):
        self.assertEqual(len(self.ok("search_entries", {}, self.owner)["items"]), 2)

    def test_get_entry_refuses_an_entry_outside_the_caller_boundary(self):
        result = self.call("get_entry", {"entry_id": str(self.private_entry.pk)}, self.outsider)
        self.assertTrue(result["isError"])
        self.assertIn("does not exist or is not readable", result["content"][0]["text"])

    def test_get_entry_is_reachable_for_everything_search_returns(self):
        # The invariant behind IP-014 Q1: an id a caller can find is an id that
        # caller can open. A stricter checker on the detail path would break it.
        for user in (AnonymousUser(), self.outsider, self.owner):
            with self.subTest(user=getattr(user, "username", "anonymous")):
                found = self.ok("search_entries", {}, user)
                for item in found["items"]:
                    self.ok("get_entry", {"entry_id": item["id"]}, user)

    def test_list_catalogs_is_scoped(self):
        self.assertEqual([c["title"] for c in self.ok("list_catalogs", {}, AnonymousUser())["items"]], ["Open Shelf"])
        self.assertEqual(len(self.ok("list_catalogs", {}, self.owner)["items"]), 2)


class EntryDetailTests(ToolTestCase):
    def test_detail_carries_the_fields_the_summary_omits(self):
        payload = self.ok("get_entry", {"entry_id": str(self.public_entry.pk)}, self.owner)["entry"]
        self.assertEqual(payload["title"], "Notes on the Analytical Engine")
        self.assertEqual(payload["publisher"], "Taylor")
        self.assertIn("created_at", payload)

    def test_missing_entry_is_a_tool_error(self):
        result = self.call("get_entry", {"entry_id": "00000000-0000-0000-0000-000000000000"}, self.owner)
        self.assertTrue(result["isError"])

    def test_long_prose_is_truncated_and_flagged(self):
        entry = self._entry(self.public_catalog, "Long One", content="x" * 20_000)
        payload = self.ok("get_entry", {"entry_id": str(entry.pk)}, self.owner)["entry"]
        self.assertTrue(payload["content_truncated"])
        self.assertLess(len(payload["content"]), 20_000)

    def test_a_non_lcp_entry_reports_no_availability_block(self):
        payload = self.ok("get_entry", {"entry_id": str(self.public_entry.pk)}, self.owner)["entry"]
        self.assertNotIn("availability", payload)


class VocabularyTests(ToolTestCase):
    def test_list_authors_is_searchable(self):
        payload = self.ok("list_authors", {"query": "Lovelace"}, self.owner)
        self.assertEqual([a["full_name"] for a in payload["items"]], ["Ada Lovelace"])

    def test_list_categories_returns_terms_and_labels(self):
        payload = self.ok("list_categories", {}, self.owner)
        self.assertEqual(payload["items"][0]["term"], "analytical-engine")
        self.assertEqual(payload["items"][0]["label"], "Analytical Engine")


class PersonalLibraryTests(ToolTestCase):
    def test_whoami_reports_the_authenticated_user(self):
        payload = self.ok("whoami", {}, self.owner)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["username"], "owner")

    def test_shelf_returns_only_the_callers_own_records(self):
        ShelfRecord.objects.create(user=self.owner, entry=self.public_entry)
        ShelfRecord.objects.create(user=self.outsider, entry=self.public_entry)

        payload = self.ok("get_my_shelf", {}, self.owner)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["entry"]["title"], "Notes on the Analytical Engine")

    def test_loans_are_empty_without_licences(self):
        self.assertEqual(self.ok("list_my_loans", {}, self.owner)["items"], [])
