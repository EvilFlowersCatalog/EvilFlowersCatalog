"""IP-034: the `dataverse` harvest connector against a mocked `DataverseClient` (canned native-API responses)."""

import copy
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import AuthSource, Catalog, Entry, User
from apps.dataverse import connector as connector_module
from apps.dataverse.connector import DataverseConnector, format_cursor, parse_cursor
from apps.dataverse.exceptions import DataverseConfigError, DataverseError, DataverseNotFoundError, DataverseStaleError
from apps.dataverse.services.client import DataverseClient
from apps.dataverse.services.mapper import dataset_to_source_record
from apps.dataverse.tests import fixtures
from apps.harvest.contract import SourceRecord, StaleVersion, registry
from apps.harvest.models import HarvestSource

PID = "doi:10.5072/FK2/BOOK42"
T0 = "2026-11-16T10:15:30Z"


def _version(pid=PID, last_update=T0, state="RELEASED"):
    version = fixtures.data("version_book_published")
    version["datasetPersistentId"] = pid
    version["lastUpdateTime"] = last_update
    version["versionState"] = state
    return version


def _released_dataset(last_update=T0):
    dataset = fixtures.data("dataset_book_draft")
    dataset["latestVersion"] = {**_version(last_update=last_update), "versionNumber": 1}
    return dataset


def _search(items, total=None):
    return {"total_count": len(items) if total is None else total, "start": 0, "items": items}


def _item(pid, updated):
    return {"type": "dataset", "global_id": pid, "updatedAt": updated, "published_at": updated}


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth, _ = AuthSource.objects.get_or_create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User.objects.create(username="lib", name="L", surname="R", auth_source=auth, is_superuser=True)
        cls.catalog = Catalog.objects.create(creator=cls.user, title="STU", url_name="stu", is_public=True)

    def setUp(self):
        self.source = HarvestSource.objects.create(
            slug="dataverse",
            name="Dataverse",
            adapter="dataverse",
            system_key="dataverse_pid",
            target_catalog=self.catalog,
            mode=HarvestSource.Mode.SYNC,
            config={"collection": "elvira"},
            enabled=True,
            oai_reexpose=False,
        )
        self.client_ = mock.Mock(spec=DataverseClient)
        self.client_.base_url = "http://dv:8080"
        self.connector = DataverseConnector()
        for name in ("reader_client", "writer_client"):
            patcher = mock.patch.object(DataverseConnector, name, return_value=self.client_)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _link(self, pid, title="Kniha"):
        return Entry.objects.create(
            creator=self.user, catalog=self.catalog, title=title, identifiers={"dataverse_pid": pid}
        )


class RegistryTests(Base):
    def test_registered_with_its_capabilities(self):
        self.assertIsInstance(registry.get("dataverse"), DataverseConnector)
        with override_settings(EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN="su"):
            self.assertEqual(self.connector.capabilities(), ["read", "write", "validate", "link"])
        with override_settings(EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN=""):
            self.assertFalse(self.connector.writable())
            self.assertEqual(self.connector.capabilities(), ["read", "validate", "link"])

    def test_config(self):
        self.assertEqual(self.connector.validate_config({"collection": "elvira", "subtree": "root"}), [])
        self.assertEqual(len(self.connector.validate_config({})), 1)
        self.assertIn("Unknown", self.connector.validate_config({"collection": "x", "foo": 1})[0])

    def test_cursor_round_trip_fits_the_column(self):
        cursor = format_cursor("2026-11-16T10:15:30.123Z", 1234)
        self.assertLessEqual(len(cursor), 40)
        self.assertEqual(parse_cursor(cursor), ("2026-11-16T10:15:30.123Z", 1234))
        self.assertEqual(parse_cursor(None), (None, 0))
        self.assertEqual(parse_cursor("2026-11-16T10:15:30Z"), ("2026-11-16T10:15:30Z", 0))


class ReaderTests(Base):
    def _fetch_by_pid(self, pid, version=":latest-published"):
        return _version(pid=pid)

    def test_incremental_pass_stops_at_the_cursor(self):
        self.source.cursor = format_cursor("2026-11-16T09:00:00Z", 0)
        self.client_.search_datasets.return_value = _search(
            [
                _item("doi:A", "2026-11-16T11:00:00Z"),
                _item("doi:B", "2026-11-16T10:00:00Z"),
                _item("doi:C", "2026-11-16T08:00:00Z"),
            ]
        )
        self.client_.get_version_by_pid.side_effect = self._fetch_by_pid
        reader = self.connector.reader(self.source)
        records = list(reader.records())
        self.assertEqual([r.record_id for r in records], ["doi:A", "doi:B"])
        self.assertTrue(all(r.version == T0 and not r.deleted for r in records))
        self.assertEqual(reader.cursor, "2026-11-16T11:00:00Z|0")
        self.assertFalse(reader.exhaustive)
        self.client_.search_datasets.assert_called_once_with(start=0, per_page=100, subtree="elvira")
        self.client_.list_versions.assert_not_called()

    def test_full_pass_pages_through_everything(self):
        self.source.cursor = format_cursor("2026-11-16T09:00:00Z", 0)
        pages = [
            _search([_item("doi:A", "2026-11-16T11:00:00Z"), _item("doi:B", "2026-11-16T08:00:00Z")], total=3),
            _search([_item("doi:C", "2026-11-16T07:00:00Z")], total=3),
        ]
        self.client_.search_datasets.side_effect = pages
        self.client_.get_version_by_pid.side_effect = self._fetch_by_pid
        with mock.patch.object(connector_module, "PAGE_SIZE", 2):
            reader = self.connector.reader(self.source, full=True)
            records = list(reader.records())
        self.assertEqual([r.record_id for r in records], ["doi:A", "doi:B", "doi:C"])
        self.assertEqual(self.client_.search_datasets.call_args_list[1].kwargs["start"], 2)

    def test_linked_datasets_deaccessioned_or_gone_are_deleted_records(self):
        self._link("doi:GONE")
        self._link("doi:DEACC")
        self._link("doi:LIVE")
        self.client_.search_datasets.return_value = _search([])

        def versions(pid, include_deaccessioned=True):
            if pid == "doi:GONE":
                raise DataverseNotFoundError("404")
            if pid == "doi:DEACC":
                return [{"versionState": "DEACCESSIONED"}]
            return [{"versionState": "RELEASED"}, {"versionState": "DRAFT"}]

        self.client_.list_versions.side_effect = versions
        reader = self.connector.reader(self.source, full=True)
        records = list(reader.records())
        self.assertEqual({r.record_id for r in records}, {"doi:GONE", "doi:DEACC"})
        self.assertTrue(all(r.deleted for r in records))
        self.assertEqual({r.extra["gone"] for r in records}, {"not found", "deaccessioned"})

    def test_withdrawals_per_pass_are_capped(self):
        for pid in ("doi:1", "doi:2", "doi:3"):
            self._link(pid)
        self.source.config = {"collection": "elvira", "max_withdrawals": 1}
        self.client_.search_datasets.return_value = _search([])
        self.client_.list_versions.side_effect = DataverseNotFoundError("404")
        records = list(self.connector.reader(self.source, full=True).records())
        self.assertEqual(len(records), 1)

    def test_server_errors_never_read_as_gone(self):
        self._link("doi:1")
        self.client_.search_datasets.return_value = _search([])
        self.client_.list_versions.side_effect = DataverseError("500")
        with self.assertRaises(DataverseError):
            list(self.connector.reader(self.source, full=True).records())

    def test_incremental_existence_checks_rotate(self):
        for pid in ("doi:1", "doi:2", "doi:3"):
            self._link(pid)
        self.source.config = {"collection": "elvira", "check_batch": 2}
        self.client_.search_datasets.return_value = _search([])
        self.client_.list_versions.return_value = [{"versionState": "RELEASED"}]
        reader = self.connector.reader(self.source)
        self.assertEqual(list(reader.records()), [])
        self.assertEqual([c.args[0] for c in self.client_.list_versions.call_args_list], ["doi:1", "doi:2"])
        self.assertEqual(reader.cursor, "|2")

        self.client_.list_versions.reset_mock()
        self.source.cursor = reader.cursor
        reader = self.connector.reader(self.source)
        list(reader.records())
        self.assertEqual([c.args[0] for c in self.client_.list_versions.call_args_list], ["doi:3", "doi:1"])

    def test_probe(self):
        self.client_.search_datasets.return_value = _search([_item(PID, T0)], total=12)
        self.client_.get_version_by_pid.side_effect = self._fetch_by_pid
        probe = self.connector.test(self.source)
        self.assertEqual(probe["identify"]["datasets"], 12)
        self.assertEqual(probe["sample"]["record_id"], PID)
        self.assertEqual(probe["errors"], [])


class FetchTests(Base):
    def test_published_version(self):
        self.client_.get_version_by_pid.return_value = _version()
        record = self.connector.fetch(self.source, PID)
        self.client_.get_version_by_pid.assert_called_once_with(PID, ":latest-published")
        self.assertEqual((record.record_id, record.version, record.deleted), (PID, T0, False))
        self.assertEqual(record.title, "Mechanika tekutín")

    def test_deaccessioned(self):
        self.client_.get_version_by_pid.side_effect = DataverseNotFoundError("404")
        self.client_.list_versions.return_value = [{"versionState": "DEACCESSIONED"}]
        record = self.connector.fetch(self.source, PID)
        self.assertTrue(record.deleted)

    def test_deaccessioned_version_served(self):
        self.client_.get_version_by_pid.return_value = _version(state="DEACCESSIONED")
        self.assertTrue(self.connector.fetch(self.source, PID).deleted)

    def test_gone(self):
        self.client_.get_version_by_pid.side_effect = DataverseNotFoundError("404")
        self.client_.list_versions.side_effect = DataverseNotFoundError("404")
        self.assertTrue(self.connector.fetch(self.source, PID).deleted)

    def test_never_published_is_an_error_not_a_deletion(self):
        self.client_.get_version_by_pid.side_effect = DataverseNotFoundError("404")
        self.client_.list_versions.return_value = [{"versionState": "DRAFT"}]
        with self.assertRaises(DataverseError):
            self.connector.fetch(self.source, PID)


@override_settings(EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN="su", EVILFLOWERS_PORTAL_URL="https://portal.example")
class PushTests(Base):
    def setUp(self):
        super().setUp()
        self.client_.get_dataset_by_pid.return_value = _released_dataset()
        self.client_.get_version_by_pid.return_value = _version(last_update="2026-11-16T12:00:00Z")
        self.remote = dataset_to_source_record(_version(), pid=PID)

    def test_update_sends_changed_fields_only_and_publishes_in_place(self):
        record = copy.deepcopy(self.remote)
        record.title = "Mechanika tekutín II"
        result = self.connector.push(self.source, PID, record, T0)

        fields, version = self.client_.edit_metadata.call_args.args[1:]
        self.assertEqual(fields, [{"typeName": "title", "value": "Mechanika tekutín II"}])
        self.assertEqual(version, T0)
        self.client_.delete_metadata.assert_not_called()
        self.client_.publish.assert_called_once_with(PID, "updatecurrent")
        self.assertEqual(result.record_id, PID)
        self.assertEqual(result.version, "2026-11-16T12:00:00Z")
        self.assertEqual(result.identifiers, {"dataverse_dataset_id": "42"})

    def test_linked_entry_gets_the_elvira_owned_fields(self):
        entry = self._link(PID)
        self.connector.push(self.source, PID, copy.deepcopy(self.remote), T0)
        sent = {field["typeName"]: field["value"] for field in self.client_.edit_metadata.call_args.args[1]}
        self.assertEqual(sent["elviraEntryId"], str(entry.pk))
        self.assertEqual(sent["elviraUrl"], f"https://portal.example/library?entry-detail-id={entry.pk}")
        self.assertNotIn("lending", sent)  # the dataset already says "open"
        self.assertEqual(sent["relatedMaterial"][0], sent["elviraUrl"])

    def test_nothing_changed_writes_nothing(self):
        result = self.connector.push(self.source, PID, copy.deepcopy(self.remote), T0)
        self.client_.edit_metadata.assert_not_called()
        self.client_.publish.assert_not_called()
        self.assertEqual(result.version, T0)

    def test_stale_expected_version(self):
        with self.assertRaises(StaleVersion):
            self.connector.push(self.source, PID, copy.deepcopy(self.remote), "2026-11-16T09:00:00Z")
        self.client_.edit_metadata.assert_not_called()

    def test_stale_refusal_from_dataverse(self):
        self.client_.edit_metadata.side_effect = DataverseStaleError("409")
        record = copy.deepcopy(self.remote)
        record.title = "Iný"
        with self.assertRaises(StaleVersion):
            self.connector.push(self.source, PID, record, T0)
        self.client_.publish.assert_not_called()

    def test_open_draft_refuses(self):
        dataset = _released_dataset()
        dataset["latestVersion"]["versionState"] = "DRAFT"
        self.client_.get_dataset_by_pid.return_value = dataset
        with self.assertRaises(DataverseError):
            self.connector.push(self.source, PID, copy.deepcopy(self.remote), T0)
        self.client_.publish.assert_not_called()

    def test_emptied_field_is_deleted(self):
        record = copy.deepcopy(self.remote)
        record.edition = None
        self.connector.push(self.source, PID, record, T0)
        self.client_.delete_metadata.assert_called_once_with(PID, [{"typeName": "edition", "value": "2. vydanie"}])

    @override_settings(EVILFLOWERS_DATAVERSE_CONTACT_EMAIL="kniznica@example.org")
    def test_create(self):
        entry = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Nová kniha")
        self.client_.create_dataset.return_value = {"id": 7, "persistentId": "doi:NEW"}
        record = SourceRecord(record_id="", title="Nová kniha", extra={"entry_id": str(entry.pk)})
        result = self.connector.push(self.source, None, record, None)

        alias, payload = self.client_.create_dataset.call_args.args
        self.assertEqual(alias, "elvira")
        self.assertEqual(payload["datasetType"], "book")
        citation = {f["typeName"]: f for f in payload["datasetVersion"]["metadataBlocks"]["citation"]["fields"]}
        book = {f["typeName"]: f for f in payload["datasetVersion"]["metadataBlocks"]["elvira_book"]["fields"]}
        self.assertEqual(
            citation["datasetContact"]["value"][0]["datasetContactEmail"]["value"], "kniznica@example.org"
        )
        self.assertEqual(book["elviraEntryId"]["value"], str(entry.pk))
        self.client_.publish.assert_called_once_with("doi:NEW", "major")
        self.assertEqual(
            (result.record_id, result.version, result.identifiers),
            ("doi:NEW", "2026-11-16T12:00:00Z", {"dataverse_dataset_id": "7"}),
        )

    @override_settings(EVILFLOWERS_DATAVERSE_CONTACT_EMAIL="", EVILFLOWERS_CONTACT_EMAIL="")
    def test_create_needs_a_contact(self):
        with self.assertRaises(DataverseConfigError):
            self.connector.push(self.source, None, SourceRecord(record_id="", title="X"), None)
        self.client_.create_dataset.assert_not_called()

    @override_settings(EVILFLOWERS_DATAVERSE_CONTACT_EMAIL="k@example.org")
    def test_created_but_not_published_still_links(self):
        self.client_.create_dataset.return_value = {"id": 7, "persistentId": "doi:NEW"}
        self.client_.publish.side_effect = DataverseError("500")
        result = self.connector.push(self.source, None, SourceRecord(record_id="", title="X"), None)
        self.assertEqual((result.record_id, result.version), ("doi:NEW", None))


class WithoutSuperuserTokenTests(TestCase):
    @override_settings(EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN="")
    def test_push_refuses(self):
        connector = DataverseConnector()
        self.assertFalse(connector.writable())
        with self.assertRaises(DataverseConfigError):
            connector.push(None, PID, SourceRecord(record_id=PID), T0)


class ValidateAndLinkTests(Base):
    def test_valid_book(self):
        self.assertEqual(self.connector.validate(self.source, dataset_to_source_record(_version())), [])

    def test_problems(self):
        version = _version()
        book = version["metadataBlocks"]["elvira_book"]["fields"]
        book[0]["value"] = ["80-227-1234-5", "12345"]
        del book[5]  # udc
        problems = self.connector.validate(self.source, dataset_to_source_record(version))
        self.assertEqual(problems, ["invalid ISBN 12345", "no UDC code (elvira_book.udc)"])

    def test_other_types_pass(self):
        record = SourceRecord(record_id=PID, extra={"dataset_type": "dataset"})
        self.assertEqual(self.connector.validate(self.source, record), [])

    def test_record_from_the_sync_projection(self):
        record = SourceRecord(record_id=PID, identifiers={"isbn": "9788022750004"})
        self.assertEqual(self.connector.validate(self.source, record), ["no UDC code (elvira_book.udc)"])

    @override_settings(EVILFLOWERS_DATAVERSE_PUBLIC_BASE="https://dv.example/")
    def test_record_url(self):
        self.assertEqual(
            self.connector.record_url(self.source, PID),
            "https://dv.example/dataset.xhtml?persistentId=doi%3A10.5072%2FFK2%2FBOOK42",
        )
