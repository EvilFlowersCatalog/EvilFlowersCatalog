"""IP-034 webhooks: the PostPublish callback (generic sync when a `dataverse` source is enabled, else the legacy
import reading `:latest-published`) and the pre-publish validation (Q9)."""

import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import AuthSource, Catalog, Entry, Language, User
from apps.dataverse.exceptions import DataverseNotFoundError, DataverseTransientError
from apps.dataverse.services.client import DataverseClient
from apps.dataverse.tests import fixtures
from apps.harvest.records import UDC_SCHEME, normalize_isbn

PID = "doi:10.5072/FK2/BOOK42"
SECRET = "s3cret"
SETTINGS = dict(
    EVILFLOWERS_DATAVERSE_WORKFLOW_SECRET=SECRET,
    EVILFLOWERS_DATAVERSE_API_TOKEN="token",
    EVILFLOWERS_DATAVERSE_BASE_INTERNAL="http://dv:8080",
    EVILFLOWERS_DATAVERSE_PUBLIC_BASE="http://dv.example",
    EVILFLOWERS_DATAVERSE_CATALOG_URL_NAME="stu",
    EVILFLOWERS_DATAVERSE_RESUME_WORKFLOW=False,
    EVILFLOWERS_PORTAL_URL="https://portal.example",
)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        source, _ = AuthSource.objects.get_or_create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(username="admin", name="A", surname="D", auth_source=source, is_superuser=True)
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="STU", url_name="stu", is_public=True)
        Language.objects.create(name="Slovak", alpha2="sk", alpha3="slk")

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")


@override_settings(**SETTINGS)
class PrepublishTests(Base):
    def _body(self, **extra):
        return {"secret": SECRET, "dataset_id": "42", "global_id": PID, **extra}

    def test_reads_latest_published_and_imports_the_book(self):
        version = fixtures.data("version_book_published")
        with (
            mock.patch.object(DataverseClient, "get_version", return_value=version) as get_version,
            mock.patch.object(DataverseClient, "get_version_files", return_value=[]) as get_files,
        ):
            response = self._post("/api/v1/dataverse-prepublish", self._body())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.content, b"OK")
        get_version.assert_called_once_with("42", ":latest-published")
        get_files.assert_called_once_with("42", ":latest-published")

        entry = Entry.objects.get(identifiers__dataverse_pid=PID)
        self.assertEqual(entry.origin, "harvest:dataverse")
        self.assertEqual(entry.title, "Mechanika tekutín")
        self.assertEqual(entry.publisher, "Spektrum STU")
        self.assertEqual(entry.edition, "2. vydanie")
        self.assertEqual(entry.page_count, 320)
        self.assertEqual(entry.identifiers["isbn"], normalize_isbn("80-227-1234-5"))
        self.assertEqual(entry.identifiers["doi"], "10.1234/fluids.2024")
        self.assertEqual(entry.identifiers["dataverse_dataset_id"], "42")
        self.assertEqual(
            [(a.surname, a.name) for a in entry.authors.order_by("entry_authors__position")],
            [("Novák", "Ján"), ("Kováčová", "Eva")],
        )
        terms = dict(entry.categories.values_list("term", "scheme"))
        self.assertEqual(
            terms, {"532": UDC_SCHEME, "621.22": UDC_SCHEME, "hydraulika": "dataverse", "prúdenie": "dataverse"}
        )
        self.assertEqual((entry.rights or {}).get("license_name"), "CC BY 4.0")

    def test_no_published_version_is_a_bad_gateway(self):
        with mock.patch.object(DataverseClient, "get_version", side_effect=DataverseNotFoundError("404")):
            response = self._post("/api/v1/dataverse-prepublish", self._body())
        self.assertEqual(response.status_code, 502)
        self.assertFalse(Entry.objects.exists())

    def test_wrong_secret(self):
        response = self._post("/api/v1/dataverse-prepublish", {**self._body(), "secret": "nope"})
        self.assertEqual(response.status_code, 403)

    def _source(self, catalog, slug="dataverse", enabled=True):
        from apps.harvest.models import HarvestSource

        return HarvestSource.objects.create(
            slug=slug,
            name=slug,
            adapter="dataverse",
            system_key="dataverse_pid",
            target_catalog=catalog,
            mode=HarvestSource.Mode.SYNC,
            config={"collection": "elvira"},
            enabled=enabled,
            oai_reexpose=False,
        )

    def test_routes_into_the_generic_sync_when_a_source_exists(self):
        source = self._source(self.catalog)
        with (
            mock.patch("apps.harvest.sync.apply_record") as apply_record,
            mock.patch("apps.dataverse.views.apply_inbound") as legacy,
        ):
            response = self._post("/api/v1/dataverse-prepublish", self._body())
        self.assertEqual(response.status_code, 200, response.content)
        apply_record.assert_called_once_with(source, PID)
        legacy.assert_not_called()

    def test_several_sources_pick_the_routed_catalog(self):
        other = Catalog.objects.create(creator=self.admin, title="Other", url_name="other")
        self._source(other, slug="dataverse-other")
        stu = self._source(self.catalog)  # EVILFLOWERS_DATAVERSE_CATALOG_URL_NAME = stu
        with mock.patch("apps.harvest.sync.apply_record") as apply_record:
            self._post("/api/v1/dataverse-prepublish", self._body())
        apply_record.assert_called_once_with(stu, PID)

    def test_disabled_source_keeps_the_legacy_import(self):
        self._source(self.catalog, enabled=False)
        with (
            mock.patch("apps.harvest.sync.apply_record") as apply_record,
            mock.patch("apps.dataverse.views.apply_inbound") as legacy,
        ):
            response = self._post("/api/v1/dataverse-prepublish", self._body())
        self.assertEqual(response.status_code, 200)
        apply_record.assert_not_called()
        legacy.assert_called_once()


@override_settings(**SETTINGS)
class ValidateTests(Base):
    URL = "/api/v1/dataverse-validate"

    def _validate(self, dataset):
        with mock.patch.object(DataverseClient, "get_dataset", return_value=dataset) as get_dataset:
            response = self._post(self.URL, {"secret": SECRET, "dataset_id": "42", "global_id": PID})
        get_dataset.assert_called_once_with("42")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        return response.content.decode()

    def _book(self):
        return fixtures.data("dataset_book_draft")

    def _book_fields(self, dataset):
        return dataset["latestVersion"]["metadataBlocks"]["elvira_book"]["fields"]

    def test_valid_book_is_ok(self):
        self.assertEqual(self._validate(self._book()), "OK")

    def test_invalid_isbn_fails(self):
        dataset = self._book()
        self._book_fields(dataset)[0]["value"] = ["80-227-1234-5", "978-80-227-5000-9"]
        self.assertEqual(self._validate(dataset), "FAILED: invalid ISBN 978-80-227-5000-9")

    def test_invalid_other_id_isbn_fails(self):
        dataset = self._book()
        other = dataset["latestVersion"]["metadataBlocks"]["citation"]["fields"][1]["value"][0]
        other["otherIdValue"]["value"] = "12345"
        self.assertEqual(self._validate(dataset), "FAILED: invalid ISBN 12345")

    def test_missing_udc_fails(self):
        dataset = self._book()
        del self._book_fields(dataset)[1]
        self.assertEqual(self._validate(dataset), "FAILED: no UDC code (elvira_book.udc)")

    def test_other_dataset_types_pass(self):
        dataset = self._book()
        dataset["datasetType"] = "dataset"
        del self._book_fields(dataset)[1]
        self.assertEqual(self._validate(dataset), "OK")

    def test_draft_read_when_latest_version_has_no_blocks(self):
        dataset = self._book()
        draft = dataset.pop("latestVersion")
        with (
            mock.patch.object(DataverseClient, "get_dataset", return_value=dataset),
            mock.patch.object(DataverseClient, "get_version", return_value=draft) as get_version,
        ):
            response = self._post(self.URL, {"secret": SECRET, "dataset_id": "42", "global_id": PID})
        get_version.assert_called_once_with("42", ":draft")
        self.assertEqual(response.content, b"OK")

    def test_unreadable_dataset_fails_closed(self):
        with mock.patch.object(DataverseClient, "get_dataset", side_effect=DataverseTransientError("down")):
            response = self._post(self.URL, {"secret": SECRET, "dataset_id": "42", "global_id": PID})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.decode().startswith("FAILED: could not read the dataset"))

    def test_wrong_secret(self):
        response = self._post(self.URL, {"secret": "nope", "dataset_id": "42", "global_id": PID})
        self.assertEqual(response.status_code, 403)
