"""IP-035: the console data grid — page, remote paging/sort/filter/search, gating, CSV, no vectors."""

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core import settings_store
from apps.core.models import AuthSource, Catalog, Entry, User
from apps.notifications.models import NotificationLog


class GridTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(
            username="grid-admin", name="G", surname="A", auth_source=source, is_superuser=True
        )
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Grid catalog", url_name="grid", is_public=True)
        cls.other = Catalog.objects.create(creator=cls.admin, title="Other catalog", url_name="other")
        for index in range(30):
            Entry.objects.create(
                creator=cls.admin,
                catalog=cls.catalog if index % 3 else cls.other,
                title=f"Book {index:02d}",
                popularity=index,
            )

    def setUp(self):
        settings_store.invalidate()
        self.addCleanup(settings_store.invalidate)
        self.client.force_login(self.admin)

    def api(self, app="core", model="entry", **params):
        return self.client.get(reverse("admin:console_grid_api", args=[app, model]), params)

    def test_secrets_never_reach_the_grid(self):
        """The admin's `exclude` (bind credentials) and passphrase hashes stay out of JSON and CSV."""
        from django.contrib import admin

        from apps.console.views.grid import columns
        from apps.readium.models import License

        AuthSource.objects.create(name="ldap", driver=AuthSource.Driver.DATABASE, content={"bind_password": "s3cr3t"})
        for model_class, model, secret in (
            (AuthSource, "authsource", "content"),
            (User, "user", "lcp_passphrase_hash"),
            (User, "user", "password"),
        ):
            names = [column["field"] for column in columns(admin.site._registry[model_class])]
            self.assertNotIn(secret, names, model)
            payload = self.api(model=model).json()
            self.assertTrue(payload["data"], model)
            self.assertTrue(all(secret not in row for row in payload["data"]), model)
        csv = b"".join(self.api(model="authsource", format="csv").streaming_content).decode()
        self.assertNotIn("s3cr3t", csv)
        self.assertNotIn("bind_password", csv)
        names = [column["field"] for column in columns(admin.site._registry[License])]
        self.assertNotIn("passphrase_hash", names)

    def test_page_renders_for_catalogs_and_entries(self):
        for model in ("catalog", "entry"):
            response = self.client.get(reverse("admin:console_grid", args=["core", model]))
            self.assertEqual(response.status_code, 200, model)
            self.assertContains(response, "tabulator.min.js")
            self.assertContains(response, 'id="grid-columns"')

    def test_changelist_links_to_the_grid(self):
        response = self.client.get(reverse("admin:core_entry_changelist"))
        self.assertContains(response, reverse("admin:console_grid", args=["core", "entry"]))

    def test_rows_are_paged(self):
        payload = self.api(size=10, page=2).json()
        self.assertEqual(payload["total"], 30)
        self.assertEqual(payload["last_page"], 3)
        self.assertEqual(len(payload["data"]), 10)
        self.assertFalse(payload["estimated"])
        row = payload["data"][0]
        self.assertTrue(row["_url"].startswith(reverse("admin:core_entry_changelist")))
        self.assertEqual(set(row["catalog"]), {"id", "label", "url"})

    def test_sort_and_filters(self):
        payload = self.api(**{"sort[0][field]": "popularity", "sort[0][dir]": "desc", "size": 10}).json()
        self.assertEqual(payload["data"][0]["title"], "Book 29")

        payload = self.api(**{"filter[0][field]": "popularity", "filter[0][type]": "like", "filter[0][value]": ">=25"})
        self.assertEqual(payload.json()["total"], 5)

        payload = self.api(**{"filter[0][field]": "catalog", "filter[0][value]": str(self.other.pk)}).json()
        self.assertEqual(payload["total"], 10)

        payload = self.api(**{"filter[0][field]": "title", "filter[0][value]": "book 1"}).json()
        self.assertEqual(payload["total"], 10)

    def test_search_uses_the_admin_search_fields(self):
        payload = self.api(q="Book 07").json()
        self.assertEqual([row["title"] for row in payload["data"]], ["Book 07"])

    def test_bad_columns_and_values_are_refused(self):
        self.assertEqual(self.api(**{"sort[0][field]": "nope", "sort[0][dir]": "asc"}).status_code, 400)
        self.assertEqual(self.api(**{"filter[0][field]": "config", "filter[0][value]": "x"}).status_code, 400)
        self.assertEqual(self.api(**{"filter[0][field]": "popularity", "filter[0][value]": ">=many"}).status_code, 400)

    def test_vectors_are_never_sent(self):
        payload = self.api(size=5).json()
        for row in payload["data"]:
            self.assertNotIn("embedding", row)
            self.assertNotIn("search_vector", row)
        page = self.client.get(reverse("admin:console_grid", args=["core", "entry"]))
        self.assertNotContains(page, '"embedding"')

    def test_csv_export(self):
        response = self.api(format="csv", **{"filter[0][field]": "catalog", "filter[0][value]": str(self.other.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        body = b"".join(response.streaming_content).decode("utf-8-sig")
        lines = [line for line in body.splitlines() if line]
        header = lines[0].split(",")
        self.assertIn("title", header)
        self.assertNotIn("embedding", header)
        self.assertNotIn("search_vector", header)
        self.assertEqual(len(lines), 11)

    @override_settings(EVILFLOWERS_ADMIN_MODULES="library")
    def test_a_disabled_module_answers_404(self):
        NotificationLog.objects.create(recipient_email="a@b.c", subject="x", notification_type="test")
        self.assertEqual(self.api("notifications", "notificationlog").status_code, 404)
        page = self.client.get(reverse("admin:console_grid", args=["notifications", "notificationlog"]))
        self.assertEqual(page.status_code, 404)
        self.assertEqual(self.api().status_code, 200)

    def test_unknown_model_answers_404(self):
        self.assertEqual(self.api("core", "nothing").status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        response = self.api()
        self.assertEqual(response.status_code, 302)
