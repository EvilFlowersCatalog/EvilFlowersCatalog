"""IP-035: record pages (hero, figures, tabs, related panel) and the changelist gallery."""

from django.test import TestCase
from django.urls import reverse

from apps.core import settings_store
from apps.core.models import AuthSource, Author, Catalog, Category, Entry, EntryAuthor, User, UserCatalog
from apps.readium.models import License


class RecordPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(
            username="rec-admin", name="Ada", surname="Admin", auth_source=source, is_superuser=True
        )
        cls.reader = User.objects.create(username="rec-reader", name="Rita", surname="Reader", auth_source=source)
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Records", url_name="records", is_public=True)
        UserCatalog.objects.create(catalog=cls.catalog, user=cls.reader, mode=UserCatalog.Mode.READ)
        cls.entry = Entry.objects.create(
            creator=cls.admin, catalog=cls.catalog, title="A record book", publisher="STU Press", popularity=42
        )
        author = Author.objects.create(catalog=cls.catalog, name="Grace", surname="Hopper")
        EntryAuthor.objects.create(entry=cls.entry, author=author, position=0)
        category = Category.objects.create(catalog=cls.catalog, creator=cls.admin, term="Compilers")
        cls.entry.categories.add(category)

    def setUp(self):
        settings_store.invalidate()
        self.addCleanup(settings_store.invalidate)
        self.client.force_login(self.admin)

    def test_entry_record(self):
        response = self.client.get(reverse("admin:core_entry_change", args=[self.entry.pk]))
        self.assertEqual(response.status_code, 200)
        record = response.context["console_record"]
        self.assertEqual(record.title, "A record book")
        self.assertIn("Grace Hopper", record.subtitle)
        self.assertIn("Popularity", [str(stat.label) for stat in record.stats])
        self.assertContains(response, 'class="record-hero"')
        self.assertContains(response, "data-record-tabs")
        self.assertContains(response, "console/js/record.js")
        # The related panel counts what points at the entry (its author row, here).
        self.assertTrue(any("authors" in str(item.label).lower() for item in record.related))

    def test_catalog_record_and_related_links_filter(self):
        response = self.client.get(reverse("admin:core_catalog_change", args=[self.catalog.pk]))
        self.assertEqual(response.status_code, 200)
        record = response.context["console_record"]
        publications = next(stat for stat in record.stats if str(stat.label) == "Publications")
        self.assertEqual(publications.value, 1)
        # Every related link opens a changelist that accepts its filter (no "?e=1" lookup error redirect).
        for item in record.related:
            follow = self.client.get(item.url)
            self.assertEqual(follow.status_code, 200, item.url)
            self.assertNotIn("e=1", follow.request.get("QUERY_STRING", ""), item.url)

    def test_user_record(self):
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        License.objects.create(
            entry=self.entry, user=self.reader, state="active", starts_at=now, expires_at=now + timedelta(days=14)
        )
        response = self.client.get(reverse("admin:core_user_change", args=[self.reader.pk]))
        self.assertEqual(response.status_code, 200)
        record = response.context["console_record"]
        self.assertEqual(record.title, "Rita Reader")
        on_loan = next(stat for stat in record.stats if str(stat.label) == "On loan")
        self.assertEqual(on_loan.value, 1)

    def test_loan_and_harvest_source_records(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.harvest.models import HarvestSource

        now = timezone.now()
        loan = License.objects.create(
            entry=self.entry, user=self.reader, state="active", starts_at=now, expires_at=now + timedelta(days=10)
        )
        response = self.client.get(reverse("admin:readium_license_change", args=[loan.pk]))
        self.assertEqual(response.status_code, 200)
        record = response.context["console_record"]
        self.assertEqual(record.title, "A record book")
        days_left = next(stat.value for stat in record.stats if str(stat.label) == "Days left")
        self.assertIn(days_left, (9, 10))  # whole days, depending on the second the test runs
        source = HarvestSource.objects.create(
            slug="partner",
            name="Partner",
            adapter="oai",
            base_url="https://oai.example.org",
            target_catalog=self.catalog,
        )
        response = self.client.get(reverse("admin:harvest_harvestsource_change", args=[source.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["console_record"].subtitle, "https://oai.example.org")

    def test_user_record_offers_set_password(self):
        response = self.client.get(reverse("admin:core_user_change", args=[self.reader.pk]))
        labels = [str(action.label) for action in response.context["console_record"].actions]
        self.assertIn("Set password", labels)

    def test_add_page_has_a_new_record_hero(self):
        response = self.client.get(reverse("admin:core_catalog_add"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("New", str(response.context["console_record"].title))

    def test_popup_keeps_the_plain_form(self):
        response = self.client.get(reverse("admin:core_catalog_change", args=[self.catalog.pk]), {"_popup": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("console_record", response.context)

    def test_gallery_view_and_its_memory(self):
        url = reverse("admin:core_entry_changelist")
        response = self.client.get(url, {"view": "gallery"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console-gallery-card")
        self.assertContains(response, "A record book")
        # Remembered for the next visit; `view` never reaches the ChangeList as a lookup.
        self.assertContains(self.client.get(url), "console-gallery-card")
        self.assertNotContains(self.client.get(url, {"view": "list"}), "console-gallery-card")

    def test_every_record_page_renders(self):
        """Models without a bespoke record get the default one (hero, related, facts)."""
        for name in ("core_author_change", "core_category_change"):
            obj = Author.objects.first() if "author" in name else Category.objects.first()
            response = self.client.get(reverse(f"admin:{name}", args=[obj.pk]))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, 'class="record-hero"')
