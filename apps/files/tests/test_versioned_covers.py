"""#128: covers and thumbnails under versioned, immutable URLs.

The version is the content hash of the stored file (`Entry.image_hash` /
`Entry.thumbnail_hash`). Only a new file changes it — never a save that bumps
`touched_at`, such as opening a book.
"""

import hashlib
import shutil
import tempfile
from io import BytesIO, StringIO

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.api.services.entry import set_cover
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User, UserAcquisition
from apps.opds.schema import AcquisitionEntry, LinkType
from apps.opds2.services.manifest_builder import ManifestBuilder

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "public, no-cache"


def png(color=(200, 30, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (600, 900), color).save(buffer, format="PNG")
    return buffer.getvalue()


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class VersionedCoverTests(TestCase):
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
        self.catalog = Catalog.objects.create(creator=self.user, title="Open", url_name="open", is_public=True)
        self.entry = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Covered")
        self.cover = png()
        set_cover(self.entry, SimpleUploadedFile("c.png", self.cover, content_type="image/png"), mime="image/png")
        self.entry.refresh_from_db()
        key = ApiKey.objects.create(user=self.user, name="test")
        self.auth = {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def thumbnail_path(self) -> str:
        return reverse("files:thumbnail-download", kwargs={"entry_id": self.entry.pk})

    # --- hashes -------------------------------------------------------------------------------------------

    def test_set_cover_stores_both_content_hashes(self):
        self.assertEqual(self.entry.image_hash, sha16(self.cover))
        with self.entry.thumbnail.open("rb") as handle:
            self.assertEqual(self.entry.thumbnail_hash, sha16(handle.read()))

    def test_a_metadata_save_keeps_the_hashes(self):
        before = (self.entry.image_hash, self.entry.thumbnail_hash)
        self.entry.title = "Renamed"
        self.entry.save()
        self.entry.refresh_from_db()
        self.assertEqual((self.entry.image_hash, self.entry.thumbnail_hash), before)

    def test_a_new_cover_rolls_the_hashes(self):
        before = (self.entry.image_hash, self.entry.thumbnail_hash)
        set_cover(
            self.entry, SimpleUploadedFile("c.png", png((10, 90, 10)), content_type="image/png"), mime="image/png"
        )
        self.entry.refresh_from_db()
        self.assertNotEqual(self.entry.image_hash, before[0])
        self.assertNotEqual(self.entry.thumbnail_hash, before[1])

    def test_removing_the_cover_clears_the_hashes(self):
        self.entry.image = None
        self.entry.thumbnail = None
        self.entry.save()
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.image_hash)
        self.assertIsNone(self.entry.thumbnail_hash)

    def test_a_reading_session_does_not_roll_the_thumbnail(self):
        acquisition = Acquisition.objects.create(entry=self.entry, mime=Acquisition.AcquisitionMIME.MOBI)
        acquisition.content.save("book.bin", ContentFile(b"MOBI" * 64), save=True)
        user_acquisition = UserAcquisition.objects.create(
            acquisition=acquisition, user=self.user, type=UserAcquisition.UserAcquisitionType.PERSONAL
        )
        self.entry.refresh_from_db()
        hashes = (self.entry.image_hash, self.entry.thumbnail_hash)
        touched = self.entry.touched_at
        etag = self.client.get(self.thumbnail_path())["ETag"]

        response = self.client.get(
            reverse("files:user-acquisition-download", kwargs={"user_acquisition_id": user_acquisition.pk}),
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)

        self.entry.refresh_from_db()
        self.assertGreater(self.entry.touched_at, touched)  # the read did save the entry
        self.assertEqual((self.entry.image_hash, self.entry.thumbnail_hash), hashes)
        again = self.client.get(self.thumbnail_path(), headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)

    # --- serving ------------------------------------------------------------------------------------------

    def test_the_current_version_is_immutable_with_a_strong_etag(self):
        response = self.client.get(self.entry.thumbnail_url, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], IMMUTABLE)
        self.assertEqual(response["ETag"], f'"{self.entry.thumbnail_hash}"')
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertNotIn("Last-Modified", response.headers)

        cover = self.client.get(self.entry.image_url)
        self.assertEqual(cover["Cache-Control"], IMMUTABLE)
        self.assertEqual(cover["ETag"], f'"{self.entry.image_hash}"')

    def test_a_revalidation_of_the_current_version_stays_immutable(self):
        etag = self.client.get(self.entry.thumbnail_url)["ETag"]
        response = self.client.get(self.entry.thumbnail_url, headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response["Cache-Control"], IMMUTABLE)

    def test_an_unversioned_url_is_revalidated(self):
        response = self.client.get(self.thumbnail_path())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], REVALIDATE)
        etag = response["ETag"]
        self.assertEqual(etag, f'"{self.entry.thumbnail_hash}"')
        again = self.client.get(self.thumbnail_path(), headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again["Cache-Control"], REVALIDATE)

    def test_a_stale_version_is_never_immutable(self):
        stale = self.entry.thumbnail_url
        set_cover(
            self.entry, SimpleUploadedFile("c.png", png((10, 10, 90)), content_type="image/png"), mime="image/png"
        )
        self.entry.refresh_from_db()
        self.assertNotEqual(stale, self.entry.thumbnail_url)

        response = self.client.get(stale)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], REVALIDATE)
        self.assertEqual(response["ETag"], f'"{self.entry.thumbnail_hash}"')
        with self.entry.thumbnail.open("rb") as handle:
            self.assertEqual(b"".join(response.streaming_content), handle.read())

    def test_a_row_without_a_hash_keeps_the_old_contract(self):
        Entry.objects.filter(pk=self.entry.pk).update(thumbnail_hash=None)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.thumbnail_url, self.thumbnail_path())

        response = self.client.get(f"{self.thumbnail_path()}?v=whatever")
        self.assertEqual(response["Cache-Control"], REVALIDATE)
        self.assertIn("Last-Modified", response.headers)

    # --- URLs ---------------------------------------------------------------------------------------------

    def test_urls_carry_the_version(self):
        self.assertEqual(self.entry.thumbnail_url, f"{self.thumbnail_path()}?v={self.entry.thumbnail_hash}")
        self.assertTrue(self.entry.image_url.endswith(f"?v={self.entry.image_hash}"))

    def test_no_thumbnail_means_no_thumbnail_url(self):
        Entry.objects.filter(pk=self.entry.pk).update(thumbnail=None, thumbnail_hash=None)
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.image_url)
        self.assertIsNone(self.entry.thumbnail_url)

    def test_rest_entries_emit_versioned_urls_and_null_without_a_thumbnail(self):
        bare = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Bare")
        response = self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.content[:300])
        items = {item["id"]: item for item in response.json()["items"]}

        covered = items[str(self.entry.pk)]
        self.assertTrue(covered["thumbnail"].endswith(f"?v={self.entry.thumbnail_hash}"))
        self.assertTrue(covered["image"].endswith(f"?v={self.entry.image_hash}"))
        self.assertIsNone(items[str(bare.pk)]["thumbnail"])
        self.assertIsNone(items[str(bare.pk)]["image"])

    def test_opds2_links_are_versioned_and_typed(self):
        links = {link.rel: link for link in ManifestBuilder._build_cover_links(self.entry, "https://x")}
        thumbnail = links[ManifestBuilder.OPDS_REL_THUMBNAIL]
        self.assertEqual(thumbnail.href, f"https://x{self.entry.thumbnail_url}")
        self.assertEqual(thumbnail.type, "image/jpeg")
        self.assertIn("?v=", links[ManifestBuilder.OPDS_REL_COVER].href)

    def test_opds1_links_are_versioned(self):
        entry = AcquisitionEntry.from_model(self.entry)
        hrefs = {link.rel: link.href for link in entry.links}
        self.assertEqual(hrefs[LinkType.THUMBNAIL], self.entry.thumbnail_url)
        self.assertEqual(hrefs[LinkType.IMAGE], self.entry.image_url)

    # --- commands -----------------------------------------------------------------------------------------

    def test_backfill_fills_missing_hashes_and_touches_the_entry(self):
        expected = (self.entry.image_hash, self.entry.thumbnail_hash)
        Entry.objects.filter(pk=self.entry.pk).update(image_hash=None, thumbnail_hash=None)
        touched = Entry.objects.get(pk=self.entry.pk).touched_at

        call_command("backfill_cover_hashes", "--dry-run", stdout=StringIO())
        self.assertIsNone(Entry.objects.get(pk=self.entry.pk).image_hash)

        call_command("backfill_cover_hashes", stdout=StringIO())
        self.entry.refresh_from_db()
        self.assertEqual((self.entry.image_hash, self.entry.thumbnail_hash), expected)
        self.assertGreater(self.entry.touched_at, touched)

    def test_regenerating_a_thumbnail_rolls_its_hash(self):
        Entry.objects.filter(pk=self.entry.pk).update(thumbnail_hash="0" * 16)
        call_command("regenerate_thumbnails", stdout=StringIO())
        self.entry.refresh_from_db()
        with self.entry.thumbnail.open("rb") as handle:
            self.assertEqual(self.entry.thumbnail_hash, sha16(handle.read()))

    def test_a_placeholder_written_under_the_missing_files_name_rolls_its_hash(self):
        # The cover and thumbnail are gone from storage, so the placeholder reuses the stored thumbnail name.
        name = self.entry.thumbnail.name
        self.entry.image.storage.delete(self.entry.image.name)
        self.entry.thumbnail.storage.delete(name)
        Entry.objects.filter(pk=self.entry.pk).update(thumbnail_hash="0" * 16)

        call_command("regenerate_thumbnails", stdout=StringIO())
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.thumbnail.name, name)
        with self.entry.thumbnail.open("rb") as handle:
            self.assertEqual(self.entry.thumbnail_hash, sha16(handle.read()))

    def test_a_cover_written_under_the_missing_files_name_rolls_its_hash(self):
        name = self.entry.image.name
        self.entry.image.storage.delete(name)
        self.entry.thumbnail.storage.delete(self.entry.thumbnail.name)
        replacement = png((90, 90, 10))

        set_cover(self.entry, SimpleUploadedFile("c.png", replacement, content_type="image/png"), mime="image/png")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.image.name, name)
        self.assertEqual(self.entry.image_hash, sha16(replacement))
        with self.entry.thumbnail.open("rb") as handle:
            self.assertEqual(self.entry.thumbnail_hash, sha16(handle.read()))

    # --- today's portal ------------------------------------------------------------------------------------

    def test_a_token_appended_by_the_portal_still_gets_the_current_body(self):
        # elvira-portal appends `?access_token=` (or `&access_token=`) until elvira-portal#38 ships.
        with self.entry.thumbnail.open("rb") as handle:
            body = handle.read()

        unversioned = self.client.get(f"{self.thumbnail_path()}?access_token=x")
        self.assertEqual(unversioned.status_code, 200)
        self.assertEqual(unversioned["Cache-Control"], REVALIDATE)
        self.assertEqual(b"".join(unversioned.streaming_content), body)

        # `?v=<hash>?access_token=x` parses as a `v` that does not match: correct body, not immutable.
        appended = self.client.get(f"{self.entry.thumbnail_url}?access_token=x")
        self.assertEqual(appended.status_code, 200)
        self.assertEqual(appended["Cache-Control"], REVALIDATE)
        self.assertEqual(b"".join(appended.streaming_content), body)

        # `?v=<hash>&access_token=x` keeps the matching `v`.
        joined = self.client.get(f"{self.entry.thumbnail_url}&access_token=x")
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(joined["Cache-Control"], IMMUTABLE)
        self.assertEqual(b"".join(joined.streaming_content), body)
