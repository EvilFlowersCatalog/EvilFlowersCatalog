"""IP-014 Phase 9: catalog management, bulk classification, feed membership.

These are the tools that let an agent *reorganise* a library rather than just
describe it, so most of what follows is about the guard rails: the catalog
boundary that keeps a category from being applied across tenants, the
all-or-nothing refusal that stops a bulk write from half-succeeding, and the
confirmation `delete_catalog` demands before it destroys a collection.
"""

from django.contrib.auth.models import AnonymousUser
from django.test import override_settings

from apps.core.models import Catalog, Category, Entry, Feed, UserCatalog
from apps.mcp.registry import ToolAccess
from apps.mcp.tests.test_management import ManagementTestCase
from apps.mcp.tools import registry


class WriteSurfaceSweepTests(ManagementTestCase):
    """Registry-driven, so a tool added later is covered without editing this file.

    The point of declaring `ToolAccess` on every tool is that authentication is
    the server's job rather than each handler's. These two sweeps are what turn
    that from a convention into something enforced.
    """

    def test_every_write_tool_refuses_an_anonymous_session(self):
        for tool in registry.available(write_enabled=True):
            if tool.access is not ToolAccess.WRITE:
                continue
            with self.subTest(tool=tool.name):
                # Arguments are deliberately empty: the access check must run
                # before anything validates them.
                self.refused(tool.name, {}, AnonymousUser())

    def _reader_arguments(self) -> dict:
        """A well-formed call per write tool, addressing something the reader can see.

        Arguments have to be valid enough to reach the permission check —
        `{"catalog_id": ...}` sent to `update_feed` comes back as "`feed_id` is
        required", which is a refusal that proves nothing about access control.
        """
        feed = Feed.objects.create(
            creator=self.manager,
            catalog=self.catalog,
            title="Sweep",
            url_name="sweep",
            kind=Feed.FeedKind.ACQUISITION,
            content="…",
            source=Feed.FeedSource.RELATION,
        )
        category = Category.objects.create(creator=self.manager, catalog=self.catalog, term="sweep")
        catalog_id, entry_ids = str(self.catalog.pk), [str(self.entry.pk)]

        return {
            "create_catalog": {"title": "Sweep", "url_name": "sweep-catalog"},
            "update_catalog": {"catalog_id": catalog_id, "title": "Sweep"},
            "delete_catalog": {"catalog_id": catalog_id, "confirm_title": self.catalog.title},
            "create_category": {"catalog_id": catalog_id, "term": "sweep-term"},
            "create_categories": {"catalog_id": catalog_id, "categories": [{"term": "sweep-term"}]},
            "update_category": {"category_id": str(category.pk), "label": "Sweep"},
            "delete_category": {"category_id": str(category.pk)},
            "create_feed": {
                "catalog_id": catalog_id,
                "title": "Sweep 2",
                "url_name": "sweep-2",
                "kind": "acquisition",
                "content": "…",
            },
            "update_feed": {"feed_id": str(feed.pk), "title": "Sweep 3"},
            "delete_feed": {"feed_id": str(feed.pk)},
            "add_entries_to_feed": {"feed_id": str(feed.pk), "entry_ids": entry_ids},
            "remove_entries_from_feed": {"feed_id": str(feed.pk), "entry_ids": entry_ids},
            "classify_entries": {"entry_ids": entry_ids, "category_ids": [str(category.pk)]},
        }

    def test_every_write_tool_refuses_a_reader_of_the_catalog(self):
        arguments = self._reader_arguments()
        writes = {tool.name for tool in registry.available(write_enabled=True) if tool.access is ToolAccess.WRITE}

        # A new write tool with no entry here fails this, which is the point:
        # the sweep must not silently stop covering the surface it describes.
        self.assertEqual(writes, set(arguments), "every write tool needs a case in _reader_arguments")

        for name in sorted(writes):
            with self.subTest(tool=name):
                message = self.refused(name, arguments[name], self.reader)
                self.assertRegex(message, "Insufficient permissions|restricted to administrators", name)


class CatalogManagementTests(ManagementTestCase):
    def test_a_superuser_can_create_a_catalog_and_manages_it_immediately(self):
        payload = self.ok("create_catalog", {"title": "New Faculty", "url_name": "new-faculty"}, self.superuser)

        self.assertEqual(payload["catalog"]["title"], "New Faculty")
        self.assertEqual(payload["catalog"]["access"], "manage")
        self.assertTrue(
            UserCatalog.objects.filter(
                catalog_id=payload["catalog"]["id"], user=self.superuser, mode=UserCatalog.Mode.MANAGE
            ).exists()
        )

    def test_managing_a_catalog_does_not_confer_the_right_to_create_one(self):
        """A catalog is a tenant boundary, so it stays an administrator action."""
        message = self.refused("create_catalog", {"title": "Mine", "url_name": "mine"}, self.manager)

        self.assertIn("administrators", message)
        self.assertFalse(Catalog.objects.filter(url_name="mine").exists())

    def test_a_taken_slug_is_a_conflict_not_a_crash(self):
        message = self.refused("create_catalog", {"title": "Clash", "url_name": "managed"}, self.superuser)
        self.assertIn("already taken", message)

    def test_a_repeated_title_is_reported_rather_than_hitting_the_constraint(self):
        # `unique_together = ("creator_id", "title")` — unchecked by the REST
        # endpoint, which guards only `url_name`.
        Catalog.objects.create(creator=self.superuser, title="Twice", url_name="twice")

        message = self.refused("create_catalog", {"title": "Twice", "url_name": "twice-again"}, self.superuser)
        self.assertIn("already own a catalog", message)

    def test_a_manager_can_rename_their_catalog(self):
        payload = self.ok("update_catalog", {"catalog_id": str(self.catalog.pk), "title": "Renamed"}, self.manager)

        self.assertEqual(payload["catalog"]["title"], "Renamed")
        self.catalog.refresh_from_db()
        self.assertEqual(self.catalog.title, "Renamed")

    def test_an_update_leaves_untouched_fields_alone(self):
        self.ok("update_catalog", {"catalog_id": str(self.catalog.pk), "is_public": True}, self.manager)

        self.catalog.refresh_from_db()
        self.assertTrue(self.catalog.is_public)
        self.assertEqual(self.catalog.url_name, "managed")

    def test_an_update_does_not_disturb_the_access_list(self):
        """`CatalogService.populate` rewrites members when handed a `users` key."""
        self.ok("update_catalog", {"catalog_id": str(self.catalog.pk), "title": "Still Shared"}, self.manager)

        self.assertTrue(UserCatalog.objects.filter(catalog=self.catalog, user=self.reader).exists())

    def test_a_reader_cannot_update_the_catalog(self):
        message = self.refused("update_catalog", {"catalog_id": str(self.catalog.pk), "title": "Nope"}, self.reader)
        self.assertIn("Insufficient permissions", message)

    def test_an_outsider_is_told_the_catalog_does_not_exist(self):
        message = self.refused("update_catalog", {"catalog_id": str(self.catalog.pk), "title": "Nope"}, self.outsider)
        self.assertIn("does not exist", message)

    def test_get_catalog_reports_what_the_caller_may_do(self):
        self.assertEqual(
            self.ok("get_catalog", {"catalog_id": str(self.catalog.pk)}, self.manager)["catalog"]["access"], "manage"
        )
        self.assertEqual(
            self.ok("get_catalog", {"catalog_id": str(self.catalog.pk)}, self.reader)["catalog"]["access"], "read"
        )

    def test_get_catalog_counts_what_is_inside(self):
        payload = self.ok("get_catalog", {"catalog_id": str(self.catalog.pk)}, self.manager)
        self.assertEqual(payload["catalog"]["entry_count"], 1)

    def test_list_catalogs_carries_the_access_level(self):
        items = self.ok("list_catalogs", {}, self.reader)["items"]
        self.assertEqual({item["title"]: item["access"] for item in items}, {"Managed": "read"})

    def test_manageable_only_narrows_the_listing(self):
        self.assertEqual(self.ok("list_catalogs", {"manageable_only": True}, self.reader)["items"], [])
        self.assertEqual(len(self.ok("list_catalogs", {"manageable_only": True}, self.manager)["items"]), 1)


class CatalogDeletionTests(ManagementTestCase):
    def test_deletion_requires_the_title_repeated_back(self):
        message = self.refused(
            "delete_catalog", {"catalog_id": str(self.catalog.pk), "confirm_title": "managed"}, self.manager
        )

        self.assertIn("does not match", message)
        self.assertTrue(Catalog.objects.filter(pk=self.catalog.pk).exists())

    def test_surrounding_whitespace_does_not_defeat_the_confirmation(self):
        # `Arguments.string` strips. A trailing space is noise, not a different
        # intent, and refusing it would train a caller to retry blindly.
        payload = self.ok(
            "delete_catalog", {"catalog_id": str(self.catalog.pk), "confirm_title": " Managed "}, self.manager
        )
        self.assertTrue(payload["deleted"])

    def test_a_confirmed_deletion_removes_the_catalog_and_reports_the_damage(self):
        payload = self.ok(
            "delete_catalog", {"catalog_id": str(self.catalog.pk), "confirm_title": "Managed"}, self.manager
        )

        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["deleted_entries"], 1)
        self.assertFalse(Catalog.objects.filter(pk=self.catalog.pk).exists())

    def test_the_permission_check_runs_before_the_confirmation_check(self):
        # Otherwise a wrong-title refusal would confirm the real title to
        # someone who may not touch the catalog at all.
        message = self.refused(
            "delete_catalog", {"catalog_id": str(self.catalog.pk), "confirm_title": "anything"}, self.outsider
        )

        self.assertIn("does not exist", message)
        self.assertNotIn("Managed", message)


class ClassificationTests(ManagementTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second_entry = Entry.objects.create(creator=cls.manager, catalog=cls.catalog, title="Another Book")
        cls.mathematics = Category.objects.create(creator=cls.manager, catalog=cls.catalog, term="51", label="Math")
        cls.physics = Category.objects.create(creator=cls.manager, catalog=cls.catalog, term="53", label="Physics")
        cls.foreign = Category.objects.create(creator=cls.outsider, catalog=cls.other_catalog, term="99")

    def test_a_manager_can_file_publications_under_categories(self):
        payload = self.ok(
            "classify_entries",
            {
                "entry_ids": [str(self.entry.pk), str(self.second_entry.pk)],
                "category_ids": [str(self.mathematics.pk)],
            },
            self.manager,
        )

        self.assertEqual(payload["changed"], 2)
        self.assertEqual(payload["unchanged"], 0)
        self.assertEqual(list(self.entry.categories.values_list("term", flat=True)), ["51"])

    def test_re_running_the_same_call_changes_nothing_and_says_so(self):
        arguments = {"entry_ids": [str(self.entry.pk)], "category_ids": [str(self.mathematics.pk)]}
        self.ok("classify_entries", arguments, self.manager)

        payload = self.ok("classify_entries", arguments, self.manager)
        self.assertEqual(payload["changed"], 0)
        self.assertEqual(payload["unchanged"], 1)

    def test_add_keeps_classifications_that_are_already_there(self):
        self.entry.categories.add(self.physics)

        self.ok(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk)], "category_ids": [str(self.mathematics.pk)], "mode": "add"},
            self.manager,
        )

        self.assertEqual(set(self.entry.categories.values_list("term", flat=True)), {"51", "53"})

    def test_replace_discards_the_rest(self):
        self.entry.categories.add(self.physics)

        self.ok(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk)], "category_ids": [str(self.mathematics.pk)], "mode": "replace"},
            self.manager,
        )

        self.assertEqual(set(self.entry.categories.values_list("term", flat=True)), {"51"})

    def test_replace_with_an_empty_list_clears_the_classification(self):
        self.entry.categories.add(self.physics)

        payload = self.ok(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk)], "category_ids": [], "mode": "replace"},
            self.manager,
        )

        self.assertEqual(payload["changed"], 1)
        self.assertEqual(self.entry.categories.count(), 0)

    def test_an_empty_list_is_refused_in_any_other_mode(self):
        message = self.refused(
            "classify_entries", {"entry_ids": [str(self.entry.pk)], "category_ids": []}, self.manager
        )
        self.assertIn("replace", message)

    def test_remove_detaches_only_what_was_named(self):
        self.entry.categories.add(self.mathematics, self.physics)

        self.ok(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk)], "category_ids": [str(self.physics.pk)], "mode": "remove"},
            self.manager,
        )

        self.assertEqual(set(self.entry.categories.values_list("term", flat=True)), {"51"})

    def test_a_category_from_another_catalog_is_refused(self):
        """Nothing in the schema prevents it, and the write would otherwise succeed."""
        message = self.refused(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk)], "category_ids": [str(self.foreign.pk)]},
            self.superuser,
        )

        self.assertIn("different catalog", message)
        self.assertEqual(self.entry.categories.count(), 0)

    def test_entries_spanning_two_catalogs_are_refused(self):
        stray = Entry.objects.create(creator=self.outsider, catalog=self.other_catalog, title="Elsewhere")

        message = self.refused(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk), str(stray.pk)], "category_ids": [str(self.mathematics.pk)]},
            self.superuser,
        )

        self.assertIn("different catalog", message)

    def test_an_unreadable_entry_id_fails_the_whole_batch(self):
        # Partial success is the worst outcome here: the caller cannot tell
        # which half landed without re-reading everything.
        stray = Entry.objects.create(creator=self.outsider, catalog=self.other_catalog, title="Hidden")

        message = self.refused(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk), str(stray.pk)], "category_ids": [str(self.mathematics.pk)]},
            self.manager,
        )

        self.assertIn("does not exist", message)
        self.assertEqual(self.entry.categories.count(), 0)

    def test_a_reader_cannot_classify(self):
        message = self.refused(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk)], "category_ids": [str(self.mathematics.pk)]},
            self.reader,
        )

        self.assertIn("Insufficient permissions", message)
        self.assertEqual(self.entry.categories.count(), 0)

    def test_an_entrys_own_creator_may_classify_it(self):
        """Mirrors `check_entry_manage`, which the REST update endpoint uses."""
        own = Entry.objects.create(creator=self.reader, catalog=self.catalog, title="Reader's Own")

        payload = self.ok(
            "classify_entries",
            {"entry_ids": [str(own.pk)], "category_ids": [str(self.mathematics.pk)]},
            self.reader,
        )

        self.assertEqual(payload["changed"], 1)

    def test_a_mixed_batch_is_refused_rather_than_partially_written(self):
        own = Entry.objects.create(creator=self.reader, catalog=self.catalog, title="Reader's Own")

        message = self.refused(
            "classify_entries",
            {"entry_ids": [str(own.pk), str(self.entry.pk)], "category_ids": [str(self.mathematics.pk)]},
            self.reader,
        )

        self.assertIn("Insufficient permissions", message)
        self.assertEqual(own.categories.count(), 0)

    def test_duplicate_ids_are_collapsed(self):
        payload = self.ok(
            "classify_entries",
            {"entry_ids": [str(self.entry.pk), str(self.entry.pk)], "category_ids": [str(self.mathematics.pk)]},
            self.manager,
        )

        self.assertEqual(len(payload["results"]), 1)

    @override_settings(EVILFLOWERS_MCP_MAX_BULK_ITEMS=1)
    def test_an_oversized_batch_is_refused_with_the_limit(self):
        message = self.refused(
            "classify_entries",
            {
                "entry_ids": [str(self.entry.pk), str(self.second_entry.pk)],
                "category_ids": [str(self.mathematics.pk)],
            },
            self.manager,
        )

        self.assertIn("at most 1", message)


class BulkCategoryTests(ManagementTestCase):
    def test_a_vocabulary_is_imported_in_one_call(self):
        payload = self.ok(
            "create_categories",
            {
                "catalog_id": str(self.catalog.pk),
                "categories": [
                    {"term": "51", "label": "Matematika", "scheme": "MDT"},
                    {"term": "53", "label": "Fyzika", "scheme": "MDT"},
                ],
            },
            self.manager,
        )

        self.assertEqual(payload["created"], 2)
        self.assertEqual(payload["skipped"], 0)
        self.assertEqual({item["term"] for item in payload["categories"]}, {"51", "53"})
        self.assertEqual(Category.objects.filter(catalog=self.catalog, scheme="MDT").count(), 2)

    def test_existing_terms_are_skipped_and_still_returned_with_their_ids(self):
        existing = Category.objects.create(creator=self.manager, catalog=self.catalog, term="51")

        payload = self.ok(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51"}, {"term": "53"}]},
            self.manager,
        )

        self.assertEqual((payload["created"], payload["skipped"]), (1, 1))
        by_term = {item["term"]: item for item in payload["categories"]}
        self.assertEqual(by_term["51"]["id"], str(existing.pk))
        self.assertFalse(by_term["51"]["created"])

    def test_skip_existing_can_be_turned_off(self):
        Category.objects.create(creator=self.manager, catalog=self.catalog, term="51")

        message = self.refused(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51"}], "skip_existing": False},
            self.manager,
        )

        self.assertIn("already has a category", message)

    def test_a_term_repeated_within_one_call_is_refused(self):
        message = self.refused(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51"}, {"term": "51"}]},
            self.manager,
        )

        self.assertIn("more than once", message)
        self.assertEqual(Category.objects.filter(catalog=self.catalog).count(), 0)

    def test_a_malformed_element_names_its_index(self):
        message = self.refused(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51"}, {"label": "no term"}]},
            self.manager,
        )

        self.assertIn("categories[1].term", message)

    def test_an_unknown_key_is_reported(self):
        message = self.refused(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51", "colour": "blue"}]},
            self.manager,
        )

        self.assertIn("colour", message)

    def test_a_reader_cannot_import_a_vocabulary(self):
        message = self.refused(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51"}]},
            self.reader,
        )

        self.assertIn("Insufficient permissions", message)
        self.assertEqual(Category.objects.filter(catalog=self.catalog).count(), 0)

    def test_nothing_is_written_when_one_element_fails(self):
        self.refused(
            "create_categories",
            {"catalog_id": str(self.catalog.pk), "categories": [{"term": "51"}, {"term": ""}]},
            self.manager,
        )

        self.assertEqual(Category.objects.filter(catalog=self.catalog).count(), 0)


class FeedMembershipTests(ManagementTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second_entry = Entry.objects.create(creator=cls.manager, catalog=cls.catalog, title="Another Book")
        cls.shelf = Feed.objects.create(
            creator=cls.manager,
            catalog=cls.catalog,
            title="Recommended",
            url_name="recommended",
            kind=Feed.FeedKind.ACQUISITION,
            content="Picks",
            source=Feed.FeedSource.RELATION,
        )
        cls.navigation = Feed.objects.create(
            creator=cls.manager,
            catalog=cls.catalog,
            title="Browse",
            url_name="browse",
            kind=Feed.FeedKind.NAVIGATION,
            content="Tree",
            source=Feed.FeedSource.RELATION,
        )

    def test_entries_are_added_without_disturbing_what_is_there(self):
        self.shelf.entries.add(self.second_entry)

        payload = self.ok(
            "add_entries_to_feed",
            {"feed_id": str(self.shelf.pk), "entry_ids": [str(self.entry.pk)]},
            self.manager,
        )

        self.assertEqual(payload["changed"], 1)
        self.assertEqual(self.shelf.entries.count(), 2)

    def test_adding_something_already_present_is_reported_not_refused(self):
        self.shelf.entries.add(self.entry)

        payload = self.ok(
            "add_entries_to_feed",
            {"feed_id": str(self.shelf.pk), "entry_ids": [str(self.entry.pk)]},
            self.manager,
        )

        self.assertEqual((payload["changed"], payload["unchanged"]), (0, 1))

    def test_removal_detaches_without_deleting_the_publication(self):
        self.shelf.entries.add(self.entry)

        payload = self.ok(
            "remove_entries_from_feed",
            {"feed_id": str(self.shelf.pk), "entry_ids": [str(self.entry.pk)]},
            self.manager,
        )

        self.assertEqual(payload["changed"], 1)
        self.assertEqual(self.shelf.entries.count(), 0)
        self.assertTrue(Entry.objects.filter(pk=self.entry.pk).exists())

    def test_a_navigation_feed_cannot_hold_publications(self):
        message = self.refused(
            "add_entries_to_feed",
            {"feed_id": str(self.navigation.pk), "entry_ids": [str(self.entry.pk)]},
            self.manager,
        )

        self.assertIn("navigation feed", message)

    def test_an_entry_from_another_catalog_is_refused(self):
        stray = Entry.objects.create(creator=self.outsider, catalog=self.other_catalog, title="Elsewhere")

        message = self.refused(
            "add_entries_to_feed",
            {"feed_id": str(self.shelf.pk), "entry_ids": [str(stray.pk)]},
            self.superuser,
        )

        self.assertIn("different catalog", message)
        self.assertEqual(self.shelf.entries.count(), 0)

    def test_a_reader_cannot_curate_a_feed(self):
        message = self.refused(
            "add_entries_to_feed",
            {"feed_id": str(self.shelf.pk), "entry_ids": [str(self.entry.pk)]},
            self.reader,
        )

        self.assertIn("Insufficient permissions", message)

    def test_an_outsider_is_not_told_the_feed_exists(self):
        message = self.refused(
            "remove_entries_from_feed",
            {"feed_id": str(self.shelf.pk), "entry_ids": [str(self.entry.pk)]},
            self.outsider,
        )

        self.assertIn("does not exist", message)
