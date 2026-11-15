"""#20 and #174 over MCP: instance settings, content policies, assignment and explain_access."""

from apps.content import policies
from apps.core import settings_store
from apps.core.models import Entry
from apps.mcp.tests.test_management import ManagementTestCase


class ContentToolTests(ManagementTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        policies.ensure_presets()

    def setUp(self):
        super().setUp()
        for cache in (settings_store, policies):
            cache.invalidate()
            self.addCleanup(cache.invalidate)

    def test_settings_are_for_superusers(self):
        for user in (self.reader, self.manager):
            self.assertIn("restricted to administrators", self.refused("get_setting", {}, user))
        listed = self.ok("get_setting", {}, self.superuser)["settings"]
        self.assertIn("content.default_policy", {item["key"] for item in listed})

    def test_update_setting_validates_dry_runs_and_resets(self):
        refused = self.refused("update_setting", {"key": "content.default_policy", "value": "nope"}, self.superuser)
        self.assertIn("no content policy 'nope'", refused)
        dry = self.ok(
            "update_setting",
            {"key": "content.default_policy", "value": "open-access", "dry_run": True},
            self.superuser,
        )
        self.assertEqual((dry["previous"], dry["value"], dry["changed"]), ("parity", "open-access", True))
        self.assertEqual(settings_store.get("content.default_policy"), "parity")
        self.ok("update_setting", {"key": "content.default_policy", "value": "open-access"}, self.superuser)
        self.assertEqual(settings_store.get("content.default_policy"), "open-access")
        reset = self.ok("update_setting", {"key": "content.default_policy", "value": None}, self.superuser)
        self.assertEqual(reset["source"], "default")
        bad = self.refused("update_setting", {"key": "content.external_processor_default", "value": 3}, self.superuser)
        self.assertIn("rule `type`", bad)

    def test_list_and_get_policies(self):
        listed = self.ok("list_content_policies", {"include_schema": True}, self.reader)
        self.assertEqual(listed["default_policy"], "parity")
        self.assertEqual(len(listed["policies"]), 5)
        self.assertIn("properties", listed["schema"])
        got = self.ok("get_content_policy", {"key": "campus-only", "include_versions": True}, self.reader)
        self.assertEqual(got["policy"]["ref"], "campus-only@1")
        self.assertEqual(len(got["versions"]), 1)

    def test_update_content_policy_is_for_superusers_and_versions(self):
        document = dict(policies.PRESETS["licensed-strict"])
        self.assertIn(
            "restricted to administrators",
            self.refused("update_content_policy", {"key": "licensed-strict", "document": document}, self.manager),
        )
        document["log"] = {"retention_days": 90}
        dry = self.ok(
            "update_content_policy", {"key": "licensed-strict", "document": document, "dry_run": True}, self.superuser
        )
        self.assertEqual(dry["policy"]["ref"], "licensed-strict@2")
        self.assertEqual(policies.current("licensed-strict").version, 1)
        saved = self.ok("update_content_policy", {"key": "licensed-strict", "document": document}, self.superuser)
        self.assertEqual((saved["policy"]["ref"], saved["created"]), ("licensed-strict@2", False))
        bad = dict(document, grants={**document["grants"], "license_states": ["borrowed"]})
        refused = self.refused("update_content_policy", {"key": "licensed-strict", "document": bad}, self.superuser)
        self.assertIn("grants/license_states/0", refused)

    def test_assign_and_explain(self):
        refused = self.refused(
            "assign_content_policy", {"key": "open-access", "catalog_id": str(self.catalog.pk)}, self.reader
        )
        self.assertIn("manage", refused)
        assigned = self.ok(
            "assign_content_policy",
            {"key": "open-access", "catalog_id": str(self.catalog.pk), "entry_ids": [str(self.entry.pk)]},
            self.manager,
        )
        self.assertTrue(assigned["catalog"]["changed"])
        self.assertEqual(Entry.objects.get(pk=self.entry.pk).config["content_policy"], "open-access")

        explained = self.ok("explain_access", {"entry_id": str(self.entry.pk)}, self.reader)
        self.assertEqual((explained["policy"], explained["policy_source"]), ("policy:open-access@1", "entry"))
        self.assertEqual(explained["tier"], "full")

        self.ok("assign_content_policy", {"key": None, "entry_ids": [str(self.entry.pk)]}, self.manager)
        explained = self.ok("explain_access", {"entry_id": str(self.entry.pk)}, self.reader)
        self.assertEqual(explained["policy_source"], "catalog")

    def test_explain_for_someone_else_is_for_superusers(self):
        arguments = {"entry_id": str(self.entry.pk), "user_id": str(self.manager.pk)}
        self.assertIn("restricted to administrators", self.refused("explain_access", arguments, self.reader))
        self.assertEqual(self.ok("explain_access", arguments, self.superuser)["user_id"], str(self.manager.pk))
