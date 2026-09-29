"""IP-014 Q12: adding publications, correcting them, and attaching their files.

The tools reuse `EntryForm` and `EntryService`, so the interesting cases are the
ones specific to MCP: what the tools refuse to touch, how a duplicate is reported
so an import can resume, and the upload link — which is a credential in a URL,
so its signature, expiry, single use and scope are all pinned down here.
"""

import shutil
import tempfile
from unittest.mock import patch
from urllib.parse import urlparse

from django.core import signing
from django.test import Client, override_settings

from apps.core.models import Acquisition, Category, Entry, Feed, Language, UserCatalog
from apps.mcp.tests.test_management import ManagementTestCase
from apps.readium.models import EncryptedContent

BROKER = "apps.readium.services.content_encryption_service.get_event_broker"
TEXT_SERVICE = "apps.dataverse.services.text_publish.TextServiceClient"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "mcp-uploads"}}


@override_settings(
    CACHES=LOCMEM,
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_READIUM_LCPSV_URL="http://lcp.example.org",
)
class PublishingTestCase(ManagementTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Language.objects.get_or_create(alpha2="sk", defaults={"alpha3": "slk", "name": "Slovak"})
        Language.objects.get_or_create(alpha2="en", defaults={"alpha3": "eng", "name": "English"})

    def setUp(self):
        super().setUp()
        self.storage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.storage, ignore_errors=True)
        for target in (BROKER, TEXT_SERVICE):
            patcher = patch(target)
            self.addCleanup(patcher.stop)
            setattr(self, "broker" if target == BROKER else "text_service", patcher.start())
        settings_override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=self.storage)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def create(self, user=None, **arguments) -> dict:
        payload = {"catalog_id": str(self.catalog.pk), "title": "Numerická matematika", "language_code": "sk"}
        payload.update(arguments)
        return self.ok("create_entry", payload, user or self.manager)["entry"]

    def upload(self, url: str, content: bytes = PDF, name="book.pdf", content_type="application/pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return Client().post(
            urlparse(url).path, {"content": SimpleUploadedFile(name, content, content_type=content_type)}
        )


class CreateEntryTests(PublishingTestCase):
    def test_a_manager_can_create_a_publication_with_full_metadata(self):
        entry = self.create(
            authors=[{"name": "Marek", "surname": "Macák"}, {"name": "Zuzana", "surname": "Minarechová"}],
            publisher="Spektrum STU",
            published_at="2021",
            identifiers={"isbn": "978-80-227-5078-3"},
            summary="Učebnica.",
        )

        self.assertEqual(entry["title"], "Numerická matematika")
        self.assertEqual(entry["authors"], ["Marek Macák", "Zuzana Minarechová"])
        self.assertEqual(entry["language"], "sk")
        self.assertEqual(entry["identifiers"], {"isbn": "978-80-227-5078-3"})
        self.assertEqual(Entry.objects.get(pk=entry["id"]).creator, self.manager)

    def test_lcp_settings_map_to_readium_config(self):
        entry = self.create(lcp_enabled=True, lcp_copies=1)

        self.assertTrue(entry["readium_enabled"])
        self.assertEqual(entry["readium_amount"], 1)
        stored = Entry.objects.get(pk=entry["id"])
        self.assertIs(stored.read_config("readium_enabled"), True)
        self.assertEqual(stored.read_config("readium_amount"), 1)

    def test_lcp_without_a_copy_count_is_refused(self):
        message = self.refused(
            "create_entry",
            {"catalog_id": str(self.catalog.pk), "title": "X", "language_code": "sk", "lcp_enabled": True},
            self.manager,
        )
        self.assertIn("lcp_copies", message)

    def test_an_open_publication_carries_no_readium_config(self):
        entry = self.create()
        self.assertNotIn("readium_enabled", entry)
        self.assertFalse(Entry.objects.get(pk=entry["id"]).read_config("readium_enabled"))

    def test_a_duplicate_title_names_the_existing_publication(self):
        message = self.refused(
            "create_entry",
            {"catalog_id": str(self.catalog.pk), "title": self.entry.title, "language_code": "sk"},
            self.manager,
        )
        self.assertIn(str(self.entry.pk), message)
        self.assertIn("edition year", message)

    def test_a_duplicate_isbn_names_the_existing_publication(self):
        first = self.create(identifiers={"isbn": "978-80-227-5078-3"})
        message = self.refused(
            "create_entry",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "Something else entirely",
                "language_code": "sk",
                "identifiers": {"isbn": "978-80-227-5078-3"},
            },
            self.manager,
        )
        self.assertIn(first["id"], message)

    def test_a_marc_language_code_is_refused_with_a_hint(self):
        message = self.refused(
            "create_entry",
            {"catalog_id": str(self.catalog.pk), "title": "X", "language_code": "slo"},
            self.manager,
        )
        self.assertIn("Language not found", message)

    def test_categories_and_feeds_from_another_catalog_are_refused(self):
        foreign = Category.objects.create(creator=self.outsider, catalog=self.other_catalog, term="x")
        message = self.refused(
            "create_entry",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "X",
                "language_code": "sk",
                "category_ids": [str(foreign.pk)],
            },
            self.manager,
        )
        self.assertIn("category_ids", message)

        feed = Feed.objects.create(
            creator=self.outsider,
            catalog=self.other_catalog,
            title="F",
            url_name="f",
            kind=Feed.FeedKind.ACQUISITION,
            content="…",
            source=Feed.FeedSource.RELATION,
        )
        message = self.refused(
            "create_entry",
            {"catalog_id": str(self.catalog.pk), "title": "Y", "language_code": "sk", "feed_ids": [str(feed.pk)]},
            self.manager,
        )
        self.assertIn("feeds", message)

    def test_other_config_switches_are_not_reachable(self):
        message = self.refused(
            "create_entry",
            {
                "catalog_id": str(self.catalog.pk),
                "title": "X",
                "language_code": "sk",
                "config": {"evilflowers_ocr_enabled": True},
            },
            self.manager,
        )
        self.assertIn("Unknown argument", message)

    def test_an_unreadable_catalog_is_reported_as_not_found(self):
        message = self.refused(
            "create_entry",
            {"catalog_id": str(self.other_catalog.pk), "title": "X", "language_code": "sk"},
            self.manager,
        )
        self.assertIn("does not exist", message)


class UpdateEntryTests(PublishingTestCase):
    def test_only_the_fields_passed_change(self):
        entry = self.create(publisher="Spektrum STU", published_at="2021", summary="Keep me.")

        updated = self.ok(
            "update_entry", {"entry_id": entry["id"], "title": "Numerická matematika (2021)"}, self.manager
        )

        self.assertEqual(updated["entry"]["title"], "Numerická matematika (2021)")
        self.assertEqual(updated["entry"]["publisher"], "Spektrum STU")
        self.assertEqual(updated["entry"]["summary"], "Keep me.")
        self.assertEqual(updated["entry"]["language"], "sk")

    def test_identifiers_are_merged_and_null_removes_one(self):
        entry = self.create(identifiers={"isbn": "978-80-227-5078-3", "doi": "10.1/x"})

        updated = self.ok("update_entry", {"entry_id": entry["id"], "identifiers": {"doi": None}}, self.manager)
        self.assertEqual(updated["entry"]["identifiers"], {"isbn": "978-80-227-5078-3"})

    def test_renaming_onto_an_existing_title_is_refused(self):
        entry = self.create()
        message = self.refused("update_entry", {"entry_id": entry["id"], "title": self.entry.title}, self.manager)
        self.assertIn(str(self.entry.pk), message)

    def test_an_empty_update_is_refused(self):
        entry = self.create()
        self.assertIn("Nothing to change", self.refused("update_entry", {"entry_id": entry["id"]}, self.manager))

    def test_enabling_lcp_on_a_publication_with_a_pdf_queues_encryption(self):
        entry = self.create()
        link = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)
        self.assertEqual(self.upload(link["upload_url"]).status_code, 201)
        self.assertFalse(EncryptedContent.objects.filter(acquisition__entry_id=entry["id"]).exists())

        self.ok("update_entry", {"entry_id": entry["id"], "lcp_enabled": True, "lcp_copies": 1}, self.manager)

        job = EncryptedContent.objects.get(acquisition__entry_id=entry["id"])
        self.assertEqual(job.status, EncryptedContent.EncryptionStatus.PENDING)
        self.broker.return_value.execute.assert_called_once()


class UploadLinkTests(PublishingTestCase):
    def test_an_lcp_publication_is_encrypted_when_its_file_arrives(self):
        entry = self.create(lcp_enabled=True, lcp_copies=1)
        link = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)
        self.assertTrue(link["lcp_enabled"])

        response = self.upload(link["upload_url"])

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["mime"], "application/pdf")
        self.assertEqual(body["encryption"]["status"], "pending")
        acquisition = Acquisition.objects.get(pk=body["acquisition_id"])
        self.assertEqual(acquisition.entry_id, Entry.objects.get(pk=entry["id"]).pk)
        self.assertEqual(acquisition.relation, Acquisition.AcquisitionType.ACQUISITION)
        self.broker.return_value.execute.assert_called_once()
        self.text_service.return_value.process_acquisition.assert_called_once()

    def test_an_open_publication_is_stored_without_encryption(self):
        entry = self.create()
        link = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)

        body = self.upload(link["upload_url"]).json()

        self.assertIsNone(body["encryption"])
        self.assertFalse(EncryptedContent.objects.exists())

    def test_the_link_works_once(self):
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)["upload_url"]

        self.assertEqual(self.upload(url).status_code, 201)
        self.assertEqual(self.upload(url).status_code, 410)
        self.assertEqual(Acquisition.objects.filter(entry_id=entry["id"]).count(), 1)

    def test_a_rejected_upload_does_not_burn_the_link(self):
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)["upload_url"]

        self.assertEqual(self.upload(url, content=b"not a pdf at all").status_code, 415)
        self.assertEqual(self.upload(url).status_code, 201)

    def test_octet_stream_falls_back_to_the_file_name(self):
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)["upload_url"]

        response = self.upload(url, content_type="application/octet-stream")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["mime"], "application/pdf")

    def test_an_expired_link_is_refused(self):
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)["upload_url"]

        with override_settings(EVILFLOWERS_MCP_UPLOAD_TTL=-1):
            self.assertEqual(self.upload(url).status_code, 410)

    def test_a_tampered_link_is_refused(self):
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)["upload_url"]
        token = urlparse(url).path.rsplit("/", 1)[1]
        forged = signing.dumps(
            {**signing.loads(token, salt="evilflowers.mcp.upload"), "e": str(self.entry.pk)}, salt="wrong"
        )

        self.assertEqual(self.upload(url.replace(token, forged)).status_code, 404)

    def test_revoked_access_stops_an_issued_link(self):
        temp = self._user("temporary")
        grant = UserCatalog.objects.create(user=temp, catalog=self.catalog, mode=UserCatalog.Mode.MANAGE)
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, temp)["upload_url"]

        grant.delete()

        self.assertEqual(self.upload(url).status_code, 403)
        self.assertFalse(Acquisition.objects.filter(entry_id=entry["id"]).exists())

    def test_a_publication_with_a_file_gets_no_second_link_by_default(self):
        entry = self.create()
        url = self.ok("create_upload_link", {"entry_id": entry["id"]}, self.manager)["upload_url"]
        self.upload(url)

        message = self.refused("create_upload_link", {"entry_id": entry["id"]}, self.manager)
        self.assertIn("already has a file", message)

        self.ok("create_upload_link", {"entry_id": entry["id"], "allow_additional": True}, self.manager)
