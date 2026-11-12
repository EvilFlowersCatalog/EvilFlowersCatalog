"""IP-027: every response says how it may be cached (#123).

Requests go through `self.client`, so the whole middleware stack — GZip,
ConditionalGet and the default policy — is under test.
"""

import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.http import HttpResponse
from django.urls import reverse

from apps.api.middleware.cache_control import CacheControlMiddleware
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


class DefaultPolicyUnitTests(SimpleTestCase):
    def run_middleware(self, method="GET", status=200, headers=None):
        def view(request):
            response = HttpResponse("body", status=status)
            for key, value in (headers or {}).items():
                response[key] = value
            return response

        request = getattr(RequestFactory(), method.lower())("/")
        return CacheControlMiddleware(view)(request)

    def test_a_read_gets_the_private_revalidating_default(self):
        response = self.run_middleware()
        self.assertEqual(response["Cache-Control"], "private, no-cache")
        self.assertIn("Authorization", response["Vary"])

    def test_writes_are_never_stored(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.run_middleware(method)["Cache-Control"], "no-store")

    def test_errors_are_never_stored(self):
        self.assertEqual(self.run_middleware(status=404)["Cache-Control"], "no-store")
        self.assertEqual(self.run_middleware(status=500)["Cache-Control"], "no-store")

    def test_a_view_policy_is_kept(self):
        response = self.run_middleware(headers={"Cache-Control": "public, max-age=60"})
        self.assertEqual(response["Cache-Control"], "public, max-age=60")
        self.assertFalse(response.has_header("Vary"))

    @override_settings(EVILFLOWERS_CACHE_CONTROL_DEFAULT="private, max-age=0, must-revalidate")
    def test_the_default_is_configurable(self):
        self.assertEqual(self.run_middleware()["Cache-Control"], "private, max-age=0, must-revalidate")


class StackTests(TestCase):
    """The default reaches real API responses, and a 304 keeps it."""

    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="reader", name="Re", surname="Ader", auth_source=source, is_superuser=True)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.catalog = Catalog.objects.create(creator=cls.user, title="Open", url_name="open", is_public=True)

    def headers(self) -> dict:
        key = ApiKey.objects.create(user=self.user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def test_the_licenses_list_must_be_revalidated(self):
        response = self.client.get("/readium/v1/licenses", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertIn("Authorization", response["Vary"])
        self.assertIn("ETag", response.headers)

    def test_a_304_carries_the_same_policy(self):
        headers = self.headers()
        first = self.client.get("/api/v1/catalogs", headers=headers)
        second = self.client.get("/api/v1/catalogs", headers={**headers, "If-None-Match": first["ETag"]})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second["Cache-Control"], first["Cache-Control"])
        self.assertIn("Authorization", second["Vary"])

    def test_a_problem_document_is_not_stored(self):
        response = self.client.get("/api/v1/catalogs/00000000-0000-0000-0000-000000000000", headers=self.headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "no-store")


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class CoverRevalidationTests(TestCase):
    """Covers may sit in shared caches, but a replaced cover shows up at once."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)

        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        self.user = User(username="reader", name="Re", surname="Ader", auth_source=source, is_superuser=True)
        self.user.set_unusable_password()
        self.user.save()
        catalog = Catalog.objects.create(creator=self.user, title="Open", url_name="open", is_public=True)
        self.entry = Entry.objects.create(creator=self.user, catalog=catalog, title="Covered", image_mime="image/png")
        self.entry.image.save("cover.png", ContentFile(PNG), save=True)
        self.url = reverse("files:cover-download", kwargs={"entry_id": self.entry.pk})
        key = ApiKey.objects.create(user=self.user, name="test")
        self.auth = {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def get(self, **headers):
        return self.client.get(self.url, headers={**self.auth, **headers})

    def test_a_cover_is_public_but_revalidated(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "public, no-cache")
        self.assertIn("ETag", response.headers)

    def test_a_matching_etag_is_a_304_without_opening_the_file(self):
        etag = self.get()["ETag"]
        storage = type(self.entry.image.storage)
        original_open = storage.open

        def refuse(*args, **kwargs):
            raise AssertionError("storage opened for a 304")

        storage.open = refuse
        try:
            response = self.get(**{"If-None-Match": etag})
        finally:
            storage.open = original_open
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response["Cache-Control"], "public, no-cache")

    def test_a_replaced_cover_changes_the_etag(self):
        etag = self.get()["ETag"]
        self.entry.image.save("cover-2.png", ContentFile(PNG[::-1]), save=True)
        response = self.get(**{"If-None-Match": etag})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response["ETag"], etag)


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class PerUserDownloadTests(TestCase):
    """`/data/v1/` answers that depend on the reader must never be shared.

    Prod nginx cached every header-less 200/201/303 under `/data/v1/` for four
    weeks, keyed by URL only. The acquisition endpoint answers a signed-in reader
    with a 303 to *their own* copy, and `format=base64` returns the file as JSON:
    both used to carry no `Cache-Control`.
    """

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)

        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        self.user = User(username="reader", name="Re", surname="Ader", auth_source=source, is_superuser=True)
        self.user.set_unusable_password()
        self.user.save()
        catalog = Catalog.objects.create(creator=self.user, title="Open", url_name="open", is_public=True)
        entry = Entry.objects.create(creator=self.user, catalog=catalog, title="Lent")
        # MOBI is served as stored (no personalisation), which keeps the test to the headers.
        acquisition = Acquisition.objects.create(entry=entry, mime=Acquisition.AcquisitionMIME.MOBI)
        acquisition.content.save("book.bin", ContentFile(b"MOBI" * 64), save=True)
        self.url = reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})
        key = ApiKey.objects.create(user=self.user, name="test")
        self.auth = {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def assertNotShared(self, response):
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("Authorization", response["Vary"])

    def test_the_redirect_to_a_personal_copy_is_private(self):
        response = self.client.get(self.url, headers=self.auth)
        self.assertEqual(response.status_code, 303)
        self.assertNotShared(response)

    def test_a_base64_download_is_private(self):
        response = self.client.get(f"{self.url}?format=base64", headers=self.auth, follow=True)
        self.assertEqual(response.status_code, 200, response.redirect_chain)
        self.assertIn("data", response.json()["response"])
        self.assertNotShared(response)
