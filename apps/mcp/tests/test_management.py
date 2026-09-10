"""IP-014: the feed and category management tools, and who may use them.

Most of this module is about refusals. A write tool that works is easy; a write
tool that refuses the right people, in a way that does not leak whether the
record exists, is the part worth pinning down.
"""

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings

from apps.core.models import AuthSource, Catalog, Category, Entry, Feed, User, UserCatalog
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry


class ManagementTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)

        cls.manager = cls._user("manager")
        cls.reader = cls._user("reader")
        cls.outsider = cls._user("outsider")
        cls.superuser = cls._user("root", is_superuser=True)

        cls.catalog = Catalog.objects.create(creator=cls.manager, title="Managed", url_name="managed")
        cls.other_catalog = Catalog.objects.create(creator=cls.outsider, title="Elsewhere", url_name="elsewhere")

        UserCatalog.objects.create(user=cls.manager, catalog=cls.catalog, mode=UserCatalog.Mode.MANAGE)
        UserCatalog.objects.create(user=cls.reader, catalog=cls.catalog, mode=UserCatalog.Mode.READ)
        UserCatalog.objects.create(user=cls.outsider, catalog=cls.other_catalog, mode=UserCatalog.Mode.MANAGE)

        cls.entry = Entry.objects.create(creator=cls.manager, catalog=cls.catalog, title="A Book")

    @classmethod
    def _user(cls, username: str, **kwargs) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=cls.auth_source, **kwargs)
        user.set_unusable_password()
        user.save()
        return user

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
        return response

    def ok(self, name: str, arguments: dict, user) -> dict:
        result = self.call(name, arguments, user)["result"]
        self.assertFalse(result.get("isError"), result["content"][0]["text"] if result.get("isError") else "")
        return result["structuredContent"]

    def refused(self, name: str, arguments: dict, user) -> str:
        result = self.call(name, arguments, user)["result"]
        self.assertTrue(result.get("isError"), f"{name} unexpectedly succeeded")
        return result["content"][0]["text"]


class FeedCreationTests(ManagementTestCase):
    def test_a_manager_can_create_a_navigation_feed(self):
        payload = self.ok(
            "create_feed",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Browse by faculty",
                "url_name": "faculty",
                "kind": "navigation",
                "content": "Top-level navigation.",
            },
            self.manager,
        )

        self.assertEqual(payload["feed"]["title"], "Browse by faculty")
        self.assertEqual(payload["feed"]["kind"], "navigation")
        self.assertTrue(payload["feed"]["opds_url"].endswith("/managed/feed/faculty"), payload["feed"]["opds_url"])

    def test_creation_records_the_caller_as_creator(self):
        payload = self.ok(
            "create_feed",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Mine",
                "url_name": "mine",
                "kind": "navigation",
                "content": "x",
            },
            self.manager,
        )
        self.assertEqual(payload["feed"]["creator_id"], str(self.manager.pk))

    def test_an_acquisition_feed_can_be_created_with_entries(self):
        payload = self.ok(
            "create_feed",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Recommended",
                "url_name": "recommended",
                "kind": "acquisition",
                "content": "Staff picks.",
                "entry_ids": [str(self.entry.pk)],
            },
            self.manager,
        )
        self.assertEqual(payload["feed"]["entry_count"], 1)
        self.assertEqual(Feed.objects.get(pk=payload["feed"]["id"]).entries.count(), 1)

    def test_source_is_populated_rather_than_left_blank(self):
        """`FeedForm` carries no `source`, so REST stores '' in a choices column."""
        payload = self.ok(
            "create_feed",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Sourced",
                "url_name": "sourced",
                "kind": "navigation",
                "content": "x",
            },
            self.manager,
        )
        self.assertEqual(Feed.objects.get(pk=payload["feed"]["id"]).source, Feed.FeedSource.RELATION)

    def test_a_navigation_feed_may_not_hold_entries(self):
        message = self.refused(
            "create_feed",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Bad",
                "url_name": "bad",
                "kind": "navigation",
                "content": "x",
                "entry_ids": [str(self.entry.pk)],
            },
            self.manager,
        )
        self.assertIn("Navigation feed cannot have entries", message)

    def test_a_duplicate_url_name_is_a_conflict(self):
        arguments = {
            "catalog_id": str(self.catalog.pk),
            "title": "First",
            "url_name": "dupe",
            "kind": "navigation",
            "content": "x",
        }
        self.ok("create_feed", arguments, self.manager)
        message = self.refused("create_feed", {**arguments, "title": "Second"}, self.manager)
        self.assertIn("url_name 'dupe' already exists", message)

    def test_a_duplicate_title_is_a_conflict_too(self):
        """`(catalog, title)` is unique. REST misses this and 500s on it."""
        arguments = {
            "catalog_id": str(self.catalog.pk),
            "title": "Same Title",
            "url_name": "one",
            "kind": "navigation",
            "content": "x",
        }
        self.ok("create_feed", arguments, self.manager)
        message = self.refused("create_feed", {**arguments, "url_name": "two"}, self.manager)
        self.assertIn("already exists", message)


class FeedPermissionTests(ManagementTestCase):
    def _create_arguments(self, catalog):
        return {
            "catalog_id": str(catalog.pk),
            "title": "Attempt",
            "url_name": "attempt",
            "kind": "navigation",
            "content": "x",
        }

    def test_read_access_is_not_enough_to_create(self):
        message = self.refused("create_feed", self._create_arguments(self.catalog), self.reader)
        self.assertIn("Insufficient permissions", message)

    def test_a_stranger_gets_not_found_rather_than_forbidden(self):
        """A refusal must not confirm that a catalog they cannot read exists."""
        message = self.refused("create_feed", self._create_arguments(self.catalog), self.outsider)
        self.assertIn("does not exist or is not readable", message)

    def test_anonymous_callers_are_refused_before_the_handler_runs(self):
        message = self.refused("create_feed", self._create_arguments(self.catalog), AnonymousUser())
        self.assertIn("authenticated session", message)

    def test_a_superuser_may_manage_any_catalog(self):
        """`has_object_permission` short-circuits on `is_superuser`.

        Worth pinning: the `check_catalog_manage` checker itself has no
        superuser branch, so this behaviour comes from the object-checker
        library rather than from anything visible in `apps/core/checkers.py`.
        MCP inherits it exactly as REST does.
        """
        payload = self.ok("create_feed", self._create_arguments(self.catalog), self.superuser)
        self.assertEqual(payload["feed"]["title"], "Attempt")

    @override_settings(EVILFLOWERS_MCP_ALLOW_WRITE=False)
    def test_the_kill_switch_hides_the_tool_entirely(self):
        response = self.call("create_feed", self._create_arguments(self.catalog), self.manager)
        # Not a tool error — the tool is not advertised, so it is simply unknown.
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unknown tool", response["error"]["message"])


class FeedUpdateTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.feed = Feed.objects.create(
            creator=self.manager,
            catalog=self.catalog,
            title="Original",
            url_name="original",
            kind=Feed.FeedKind.ACQUISITION,
            source=Feed.FeedSource.RELATION,
            content="Original description.",
        )

    def test_a_partial_update_leaves_other_fields_alone(self):
        payload = self.ok("update_feed", {"feed_id": str(self.feed.pk), "title": "Renamed"}, self.manager)

        self.assertEqual(payload["feed"]["title"], "Renamed")
        self.assertEqual(payload["feed"]["url_name"], "original")
        self.assertEqual(payload["feed"]["content"], "Original description.")

    def test_entry_ids_replaces_the_whole_set(self):
        self.feed.entries.add(self.entry)
        second = Entry.objects.create(creator=self.manager, catalog=self.catalog, title="Another")

        self.ok("update_feed", {"feed_id": str(self.feed.pk), "entry_ids": [str(second.pk)]}, self.manager)

        self.assertEqual(list(self.feed.entries.values_list("id", flat=True)), [second.pk])

    def test_omitting_entry_ids_leaves_the_set_intact(self):
        self.feed.entries.add(self.entry)
        self.ok("update_feed", {"feed_id": str(self.feed.pk), "title": "Touched"}, self.manager)
        self.assertEqual(self.feed.entries.count(), 1)

    def test_a_reader_cannot_update(self):
        self.assertIn(
            "Insufficient permissions",
            self.refused("update_feed", {"feed_id": str(self.feed.pk), "title": "Nope"}, self.reader),
        )

    def test_moving_into_an_unmanaged_catalog_is_refused(self):
        """The REST endpoint permits this; it checks only the source catalog."""
        message = self.refused(
            "update_feed",
            {"feed_id": str(self.feed.pk), "catalog_id": str(self.other_catalog.pk)},
            self.manager,
        )
        self.assertIn("does not exist or is not readable", message)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.catalog_id, self.catalog.pk)

    def test_a_feed_cannot_become_its_own_parent(self):
        message = self.refused(
            "update_feed",
            {"feed_id": str(self.feed.pk), "parent_ids": [str(self.feed.pk)]},
            self.manager,
        )
        self.assertIn("Invalid arguments", message)


class FeedDeletionTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.feed = Feed.objects.create(
            creator=self.manager,
            catalog=self.catalog,
            title="Doomed",
            url_name="doomed",
            kind=Feed.FeedKind.ACQUISITION,
            source=Feed.FeedSource.RELATION,
            content="x",
        )
        self.feed.entries.add(self.entry)

    def test_deleting_a_feed_reports_what_went(self):
        payload = self.ok("delete_feed", {"feed_id": str(self.feed.pk)}, self.manager)
        self.assertEqual(payload, {"deleted": True, "id": str(self.feed.pk), "title": "Doomed"})
        self.assertFalse(Feed.objects.filter(pk=self.feed.pk).exists())

    def test_the_publications_inside_survive(self):
        self.ok("delete_feed", {"feed_id": str(self.feed.pk)}, self.manager)
        self.assertTrue(Entry.objects.filter(pk=self.entry.pk).exists())

    def test_a_reader_cannot_delete(self):
        self.refused("delete_feed", {"feed_id": str(self.feed.pk)}, self.reader)
        self.assertTrue(Feed.objects.filter(pk=self.feed.pk).exists())


class FeedReadTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.feed = Feed.objects.create(
            creator=self.manager,
            catalog=self.catalog,
            title="Visible",
            url_name="visible",
            kind=Feed.FeedKind.NAVIGATION,
            source=Feed.FeedSource.RELATION,
            content="x",
        )

    def test_a_member_can_list_feeds(self):
        payload = self.ok("list_feeds", {}, self.reader)
        self.assertEqual([item["title"] for item in payload["items"]], ["Visible"])

    def test_an_outsider_sees_none(self):
        self.assertEqual(self.ok("list_feeds", {}, self.outsider)["items"], [])

    def test_get_feed_is_refused_for_an_outsider(self):
        self.assertIn(
            "does not exist or is not readable",
            self.refused("get_feed", {"feed_id": str(self.feed.pk)}, self.outsider),
        )


class CategoryManagementTests(ManagementTestCase):
    def test_a_manager_can_create_a_category(self):
        payload = self.ok(
            "create_category",
            {"catalog_id": str(self.catalog.pk), "term": "informatics", "label": "Informatics"},
            self.manager,
        )
        self.assertEqual(payload["category"]["term"], "informatics")
        self.assertEqual(payload["category"]["label"], "Informatics")

    def test_a_duplicate_term_is_a_conflict(self):
        arguments = {"catalog_id": str(self.catalog.pk), "term": "maths"}
        self.ok("create_category", arguments, self.manager)
        self.assertIn("already has a category with the term", self.refused("create_category", arguments, self.manager))

    def test_the_same_term_is_fine_in_a_different_catalog(self):
        self.ok("create_category", {"catalog_id": str(self.catalog.pk), "term": "shared"}, self.manager)
        self.ok("create_category", {"catalog_id": str(self.other_catalog.pk), "term": "shared"}, self.outsider)

    def test_a_reader_cannot_create(self):
        self.assertIn(
            "Insufficient permissions",
            self.refused("create_category", {"catalog_id": str(self.catalog.pk), "term": "nope"}, self.reader),
        )

    def test_a_partial_update_leaves_other_fields_alone(self):
        created = self.ok(
            "create_category",
            {"catalog_id": str(self.catalog.pk), "term": "physics", "label": "Physics", "scheme": "http://x"},
            self.manager,
        )["category"]

        payload = self.ok("update_category", {"category_id": created["id"], "label": "Applied Physics"}, self.manager)

        self.assertEqual(payload["category"]["label"], "Applied Physics")
        self.assertEqual(payload["category"]["term"], "physics")
        self.assertEqual(payload["category"]["scheme"], "http://x")

    def test_deleting_reports_how_many_publications_were_unfiled(self):
        created = self.ok("create_category", {"catalog_id": str(self.catalog.pk), "term": "doomed"}, self.manager)[
            "category"
        ]
        self.entry.categories.add(Category.objects.get(pk=created["id"]))

        payload = self.ok("delete_category", {"category_id": created["id"]}, self.manager)

        self.assertEqual(payload["deleted"], True)
        self.assertEqual(payload["unfiled_entries"], 1)
        # The publication itself is untouched — it just loses the label.
        self.assertTrue(Entry.objects.filter(pk=self.entry.pk).exists())
        self.assertEqual(self.entry.categories.count(), 0)

    def test_a_reader_cannot_delete(self):
        created = self.ok("create_category", {"catalog_id": str(self.catalog.pk), "term": "safe"}, self.manager)[
            "category"
        ]
        self.refused("delete_category", {"category_id": created["id"]}, self.reader)
        self.assertTrue(Category.objects.filter(pk=created["id"]).exists())

    def test_an_outsider_gets_not_found(self):
        created = self.ok("create_category", {"catalog_id": str(self.catalog.pk), "term": "hidden"}, self.manager)[
            "category"
        ]
        self.assertIn(
            "does not exist or is not readable",
            self.refused("delete_category", {"category_id": created["id"]}, self.outsider),
        )


class AtomicityTests(ManagementTestCase):
    def test_a_rejected_create_leaves_nothing_behind(self):
        before = Feed.objects.count()
        self.refused(
            "create_feed",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Bad",
                "url_name": "bad",
                "kind": "navigation",
                "content": "x",
                "entry_ids": [str(self.entry.pk)],
            },
            self.manager,
        )
        self.assertEqual(Feed.objects.count(), before)
