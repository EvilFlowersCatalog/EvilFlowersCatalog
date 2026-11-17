"""IP-035: create, update and delete through the console, with the REST rules each model has."""

from io import BytesIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.content.models import ContentPolicy
from apps.core import settings_store
from apps.core.models import (
    Acquisition,
    ApiKey,
    AuthSource,
    Author,
    Catalog,
    Category,
    Entry,
    EntryAuthor,
    EntryReview,
    Feed,
    User,
    UserCatalog,
)
from apps.harvest.models import HarvestSource


def management_data(response):
    """Every inline formset's management form (no extra rows), as a POST would carry it."""
    data = {}
    for inline in response.context["inline_admin_formsets"]:
        formset = inline.formset
        prefix = formset.prefix
        data[f"{prefix}-TOTAL_FORMS"] = "0"
        data[f"{prefix}-INITIAL_FORMS"] = "0"
        data[f"{prefix}-MIN_NUM_FORMS"] = "0"
        data[f"{prefix}-MAX_NUM_FORMS"] = "1000"
    return data


def png_upload(name="cover.png"):
    buffer = BytesIO()
    Image.new("RGB", (40, 60), (168, 3, 49)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class CrudTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(
            username="crud-admin", name="C", surname="A", auth_source=cls.source, is_superuser=True
        )
        cls.reader = User.objects.create(username="crud-reader", name="R", surname="R", auth_source=cls.source)
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Main", url_name="main")

    def setUp(self):
        settings_store.invalidate()
        self.addCleanup(settings_store.invalidate)
        self.client.force_login(self.admin)

    def add(self, app, model, data):
        url = reverse(f"admin:{app}_{model}_add")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        payload = {**management_data(page), **data}
        return self.client.post(url, payload)

    def assertSaved(self, response):
        if response.status_code != 302:
            errors = response.context and (
                response.context.get("errors") or getattr(response.context.get("adminform"), "form", None).errors
            )
            self.fail(f"Not saved ({response.status_code}): {errors}")


class CatalogCrudTests(CrudTestCase):
    def test_add_grants_the_creator_manage(self):
        response = self.add("core", "catalog", {"title": "New catalog", "url_name": "new-catalog", "is_public": "on"})
        self.assertSaved(response)
        catalog = Catalog.objects.get(url_name="new-catalog")
        self.assertEqual(catalog.creator, self.admin)
        self.assertTrue(
            UserCatalog.objects.filter(catalog=catalog, user=self.admin, mode=UserCatalog.Mode.MANAGE).exists()
        )

    def test_change_and_delete(self):
        catalog = Catalog.objects.create(creator=self.admin, title="Temp", url_name="temp")
        url = reverse("admin:core_catalog_change", args=[catalog.pk])
        page = self.client.get(url)
        response = self.client.post(url, {**management_data(page), "title": "Renamed", "url_name": "temp"})
        self.assertSaved(response)
        catalog.refresh_from_db()
        self.assertEqual(catalog.title, "Renamed")
        response = self.client.post(reverse("admin:core_catalog_delete", args=[catalog.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Catalog.objects.filter(pk=catalog.pk).exists())

    def test_unknown_content_policy_is_refused(self):
        response = self.add("core", "catalog", {"title": "X", "url_name": "x", "content_policy": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Catalog.objects.filter(url_name="x").exists())


class EntryCrudTests(CrudTestCase):
    def test_add_with_cover_and_author(self):
        author = Author.objects.create(catalog=self.catalog, name="Ada", surname="Lovelace")
        url = reverse("admin:core_entry_add")
        page = self.client.get(url)
        data = management_data(page)
        prefix = next(
            inline.formset.prefix
            for inline in page.context["inline_admin_formsets"]
            if inline.formset.model is EntryAuthor
        )
        data.update(
            {
                f"{prefix}-TOTAL_FORMS": "1",
                f"{prefix}-0-position": "0",
                f"{prefix}-0-author": str(author.pk),
                "title": "Notes on the Analytical Engine",
                "catalog": str(self.catalog.pk),
                "rights": "{}",
                "config": "{}",
                "cover_upload": png_upload(),
            }
        )
        response = self.client.post(url, data)
        self.assertSaved(response)
        entry = Entry.objects.get(title="Notes on the Analytical Engine")
        self.assertEqual(entry.creator, self.admin)
        self.assertEqual(list(entry.authors.all()), [author])
        self.assertTrue(entry.thumbnail)
        self.assertEqual(entry.image_mime, "image/png")

    def test_duplicate_title_in_the_catalog_is_refused(self):
        Entry.objects.create(creator=self.admin, catalog=self.catalog, title="Twice")
        response = self.add(
            "core", "entry", {"title": "Twice", "catalog": str(self.catalog.pk), "rights": "{}", "config": "{}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Entry.objects.filter(title="Twice").count(), 1)

    @mock.patch("apps.api.services.entry.queue_ocr")
    @mock.patch("apps.api.services.entry.store_table_of_contents")
    @mock.patch("apps.extraction.services.queue")
    def test_file_upload_goes_through_attach_acquisition(self, extraction_queue, _toc, ocr):
        entry = Entry.objects.create(creator=self.admin, catalog=self.catalog, title="With a file")
        upload = SimpleUploadedFile("book.pdf", b"%PDF-1.4\n%fake\n", content_type="application/pdf")
        response = self.add(
            "core",
            "acquisition",
            {"entry": str(entry.pk), "relation": "acquisition", "mime": "application/pdf", "upload": upload},
        )
        self.assertSaved(response)
        acquisition = Acquisition.objects.get(entry=entry)
        self.assertTrue(acquisition.content)
        extraction_queue.assert_called_once()
        ocr.assert_called_once()

    def test_a_file_that_is_not_its_type_is_refused(self):
        entry = Entry.objects.create(creator=self.admin, catalog=self.catalog, title="Bad file")
        upload = SimpleUploadedFile("book.pdf", b"not a pdf", content_type="application/pdf")
        response = self.add(
            "core",
            "acquisition",
            {"entry": str(entry.pk), "relation": "acquisition", "mime": "application/pdf", "upload": upload},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Acquisition.objects.filter(entry=entry).exists())

    def test_delete_entry(self):
        entry = Entry.objects.create(creator=self.admin, catalog=self.catalog, title="Gone")
        response = self.client.post(reverse("admin:core_entry_delete", args=[entry.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Entry.objects.filter(pk=entry.pk).exists())


class LibraryCrudTests(CrudTestCase):
    def test_category_add_records_the_creator(self):
        response = self.add("core", "category", {"term": "Algebra", "catalog": str(self.catalog.pk), "label": "{}"})
        self.assertSaved(response)
        self.assertEqual(Category.objects.get(term="Algebra").creator, self.admin)

    def test_author_add(self):
        response = self.add("core", "author", {"name": "Alan", "surname": "Turing", "catalog": str(self.catalog.pk)})
        self.assertSaved(response)

    def test_feed_slug_clash_is_refused_with_a_suggestion(self):
        Feed.objects.create(
            catalog=self.catalog, creator=self.admin, title={"en": "A"}, url_name="new", kind="navigation"
        )
        response = self.add(
            "core",
            "feed",
            {
                "catalog": str(self.catalog.pk),
                "title": '{"en": "B"}',
                "url_name": "new",
                "kind": "navigation",
                "per_page": "20",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Already used in this catalog")


class AccountCrudTests(CrudTestCase):
    def test_nobody_deletes_themselves(self):
        response = self.client.get(reverse("admin:core_user_delete", args=[self.admin.pk]))
        self.assertEqual(response.status_code, 403)

    def test_delete_other_user(self):
        victim = User.objects.create(username="victim", name="V", surname="V", auth_source=self.source)
        response = self.client.post(reverse("admin:core_user_delete", args=[victim.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=victim.pk).exists())


class ContentPolicyCrudTests(CrudTestCase):
    def test_invalid_policy_document_is_refused(self):
        response = self.add("content", "contentpolicy", {"key": "broken", "document": '{"title": 1}'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContentPolicy.objects.filter(key="broken").exists())

    def test_saving_a_policy_writes_the_next_version(self):
        from apps.content import policies

        policies.ensure_presets()
        existing = ContentPolicy.objects.order_by("key", "-version").first()
        document = {**existing.document, "title": existing.document["title"] + " (edited)"}
        url = reverse("admin:content_contentpolicy_change", args=[existing.pk])
        import json

        response = self.client.post(url, {"document": json.dumps(document)})
        self.assertSaved(response)
        self.assertTrue(ContentPolicy.objects.filter(key=existing.key, version=existing.version + 1).exists())
        self.assertTrue(ContentPolicy.objects.filter(pk=existing.pk).exists())


class HarvestSourceCrudTests(CrudTestCase):
    def test_unknown_adapter_and_missing_base_url_are_refused(self):
        response = self.add(
            "harvest",
            "harvestsource",
            {"name": "Partner", "slug": "partner", "adapter": "oai", "mode": "import", "config": "{}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(HarvestSource.objects.filter(slug="partner").exists())


@override_settings(EVILFLOWERS_ADMIN_READ_ONLY=True)
class ReadOnlyDeploymentTests(CrudTestCase):
    def test_no_add_and_no_actions(self):
        self.assertEqual(self.client.get(reverse("admin:core_catalog_add")).status_code, 403)
        review = EntryReview.objects.create(
            entry=Entry.objects.create(creator=self.admin, catalog=self.catalog, title="R"), user=self.reader, rating=3
        )
        self.client.post(
            reverse("admin:core_entryreview_changelist"), {"action": "hide_reviews", "_selected_action": [review.pk]}
        )
        review.refresh_from_db()
        self.assertEqual(review.state, EntryReview.State.PUBLISHED)

    def test_api_keys_cannot_be_revoked(self):
        key = ApiKey.objects.create(user=self.admin, name="k")
        self.client.post(reverse("admin:core_apikey_changelist"), {"action": "revoke", "_selected_action": [key.pk]})
        key.refresh_from_db()
        self.assertTrue(key.is_active)
