"""The admin API (IP-034): sources CRUD, test, upload, records, external systems, adapters; who may do what."""

import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey
from apps.harvest.models import HarvestRecord, HarvestSource
from apps.harvest.tests.base import ARL_PAGES, FakeSession, HarvestTestCase, fixture

URL = "/api/v1/harvest/sources"


class ApiTestCase(HarvestTestCase):
    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def payload(self, **kwargs) -> dict:
        data = {
            "slug": "arl-stu",
            "name": "ARL STU",
            "adapter": "oai_pmh",
            "base_url": "https://kis.example.org/oai",
            "system_key": "arl_stu",
            "target_catalog_id": str(self.catalog.pk),
        }
        data.update(kwargs)
        return data

    def post(self, user, data, url=URL):
        return self.client.post(url, data=data, content_type="application/json", headers=self.headers(user))


class SourceCrudTests(ApiTestCase):
    def test_a_manager_creates_a_disabled_link_only_source(self):
        response = self.post(self.manager, self.payload(schedule="0 3 1 * *", field_policy={"title": "overwrite"}))

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()["response"]
        self.assertEqual(body["slug"], "arl-stu")
        self.assertEqual((body["mode"], body["enabled"], body["metadata_prefix"]), ("link_only", False, "marc21"))
        self.assertEqual(body["record_counts"], {})
        self.assertEqual(body["config"], {})
        self.assertEqual(HarvestSource.objects.get().creator, self.manager)

    def test_a_manager_of_another_catalog_is_refused(self):
        self.assertEqual(self.post(self.outsider, self.payload()).status_code, 403)

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_unknown_system_key_adapter_and_bad_schedule_are_validation_errors(self):
        for change in ({"system_key": "isbn"}, {"adapter": "ftp"}, {"schedule": "every day"}, {"base_url": None}):
            response = self.post(self.manager, self.payload(**change))
            self.assertEqual(response.status_code, 422, (change, response.content))

    def test_credentials_in_config_are_refused(self):
        response = self.post(self.manager, self.payload(config={"api_token": "secret"}))
        self.assertEqual(response.status_code, 422, response.content)
        response = self.post(self.manager, self.payload(config={"token_setting": "EVILFLOWERS_X_TOKEN"}))
        self.assertEqual(response.status_code, 201, response.content)

    def test_duplicate_slug_is_a_conflict(self):
        self.source()
        self.assertEqual(self.post(self.manager, self.payload()).status_code, 409)

    def test_list_shows_only_sources_of_managed_catalogs(self):
        mine = self.source()
        self.source(slug="other", target_catalog=self.other_catalog)
        response = self.client.get(URL, headers=self.headers(self.manager))
        self.assertEqual([item["id"] for item in response.json()["items"]], [str(mine.pk)])
        response = self.client.get(URL, headers=self.headers(self.superuser))
        self.assertEqual(response.json()["metadata"]["total"], 2)

    def test_detail_of_a_foreign_source_is_not_found(self):
        source = self.source(slug="other", target_catalog=self.other_catalog)
        self.assertEqual(self.client.get(f"{URL}/{source.pk}", headers=self.headers(self.manager)).status_code, 404)

    def test_update_keeps_the_slug_and_unsent_fields(self):
        source = self.source(schedule="0 3 * * *", field_policy={"title": "overwrite"})
        response = self.client.put(
            f"{URL}/{source.pk}",
            data=self.payload(name="Renamed", enabled=True, cursor=None),
            content_type="application/json",
            headers=self.headers(self.manager),
        )
        self.assertEqual(response.status_code, 200, response.content)
        source.refresh_from_db()
        self.assertEqual((source.name, source.enabled, source.schedule), ("Renamed", True, "0 3 * * *"))
        self.assertEqual(source.field_policy, {"title": "overwrite"})

        response = self.client.put(
            f"{URL}/{source.pk}",
            data=self.payload(slug="renamed"),
            content_type="application/json",
            headers=self.headers(self.manager),
        )
        self.assertEqual(response.status_code, 422)

    def test_delete(self):
        source = self.source()
        response = self.client.delete(f"{URL}/{source.pk}", headers=self.headers(self.manager))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(HarvestSource.objects.exists())


class RecordsAndProbeTests(ApiTestCase):
    def test_records_filter_by_status_dry_run_and_query(self):
        source = self.source()
        HarvestRecord.objects.create(source=source, record_id="1", status="matched", source_record={"title": "Alfa"})
        HarvestRecord.objects.create(source=source, record_id="2", status="ambiguous", source_record={"title": "Beta"})
        HarvestRecord.objects.create(source=source, record_id="2", status="matched", dry_run=True)

        def ids(query):
            response = self.client.get(f"{URL}/{source.pk}/records?{query}", headers=self.headers(self.manager))
            self.assertEqual(response.status_code, 200, response.content)
            return sorted((item["record_id"], item["dry_run"]) for item in response.json()["items"])

        self.assertEqual(ids("status=ambiguous"), [("2", False)])
        self.assertEqual(ids("dry_run=true"), [("2", True)])
        self.assertEqual(ids("dry_run=false&query=alf"), [("1", False)])
        self.assertEqual(len(ids("")), 3)

    def test_records_of_a_foreign_source_are_not_found(self):
        source = self.source(slug="other", target_catalog=self.other_catalog)
        response = self.client.get(f"{URL}/{source.pk}/records", headers=self.headers(self.manager))
        self.assertEqual(response.status_code, 404)

    def test_probe_writes_nothing(self):
        source = self.source()
        with patch("apps.harvest.adapters.oai.requests.Session", return_value=FakeSession(ARL_PAGES)):
            response = self.client.post(f"{URL}/{source.pk}/test", headers=self.headers(self.manager))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["response"]
        self.assertEqual(body["identify"]["repositoryName"], "Example ARL catalogue")
        self.assertEqual(body["sample"]["record_id"], "0000101")
        self.assertFalse(HarvestRecord.objects.exists())
        source.refresh_from_db()
        self.assertIsNone(source.cursor)

    def test_external_systems_and_adapters(self):
        response = self.client.get("/api/v1/external-systems", headers=self.headers(self.manager))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["key"] for item in response.json()["response"]], ["arl_stu", "partner"])
        response = self.client.get("/api/v1/harvest/adapters", headers=self.headers(self.manager))
        names = {item["name"]: item for item in response.json()["response"]}
        self.assertTrue(names["marc_file"]["needs_upload"])
        self.assertTrue(names["oai_pmh"]["needs_base_url"])


class UploadTests(ApiTestCase):
    def setUp(self):
        self.storage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.storage, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=self.storage)
        override.enable()
        self.addCleanup(override.disable)

    def upload(self, source, content, user=None):
        return self.client.post(
            f"{URL}/{source.pk}/upload",
            {"file": SimpleUploadedFile("delivery.xml", content, content_type="application/xml")},
            headers=self.headers(user or self.manager),
        )

    def test_upload_then_run_a_marc_file_source(self):
        from apps.harvest.runner import run_source

        source = self.source(slug="partner", adapter="marc_file", base_url=None, system_key="partner", mode="create")
        response = self.upload(source, fixture("delivery.xml"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["response"]["has_upload"])

        source.refresh_from_db()
        report = run_source(source)

        self.assertEqual(report["created"], 2, report)

    def test_upload_to_an_oai_source_is_a_conflict(self):
        self.assertEqual(self.upload(self.source(), fixture("delivery.xml")).status_code, 409)

    def test_not_marc_is_refused(self):
        source = self.source(slug="partner", adapter="marc_file", base_url=None)
        self.assertEqual(self.upload(source, b"%PDF-1.7 nope").status_code, 415)
