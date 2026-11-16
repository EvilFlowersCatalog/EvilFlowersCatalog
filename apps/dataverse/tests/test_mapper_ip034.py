"""IP-034: Dataverse dataset JSON <-> `SourceRecord`, and the payloads Elvira writes back (`elvira_book` too)."""

from django.test import TestCase, override_settings

from apps.core.models import AuthSource, Author, Catalog, Category, Entry, EntryAuthor, Language, User
from apps.dataverse.services.mapper import (
    create_payload,
    dataset_to_source_record,
    edit_payload,
    entry_to_source_record,
    extract_metadata,
    record_to_values,
)
from apps.dataverse.tests import fixtures
from apps.harvest.records import UDC_SCHEME, normalize_isbn

PID = "doi:10.5072/FK2/BOOK42"


def _version_from_create(payload: dict) -> dict:
    """What Dataverse would serve back for a dataset created with `payload` (blocks as sent)."""
    version = payload["datasetVersion"]
    return {
        "datasetPersistentId": PID,
        "versionState": "RELEASED",
        "lastUpdateTime": "2026-11-16T12:00:00Z",
        "license": version.get("license"),
        "metadataBlocks": version["metadataBlocks"],
    }


class InboundMappingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Language.objects.create(name="Slovak", alpha2="sk", alpha3="slk")

    def setUp(self):
        self.record = dataset_to_source_record(fixtures.data("version_book_published"))

    def test_citation_fields(self):
        record = self.record
        self.assertEqual(record.record_id, PID)
        self.assertEqual(record.title, "Mechanika tekutín")  # whitespace collapsed
        self.assertEqual([(a.surname, a.name) for a in record.authors], [("Novák", "Ján"), ("Kováčová", "Eva")])
        self.assertEqual(record.summary, "Učebnica mechaniky tekutín.")
        self.assertEqual(record.publisher, "Spektrum STU")
        self.assertEqual(record.published, "2024-03")
        self.assertEqual(record.language, "sk")  # Dataverse's English name, as ISO 639-1
        self.assertEqual(record.version, "2026-11-16T10:15:30Z")
        self.assertEqual(record.links, ["https://example.org/errata"])
        self.assertEqual(
            (record.licence_name, record.licence_url), ("CC BY 4.0", "http://creativecommons.org/licenses/by/4.0")
        )
        self.assertEqual(record.datestamp, "2026-11-16T10:15:30Z")
        self.assertEqual(record.extra["dataset_id"], "42")
        self.assertEqual(record.extra["other_ids"], [{"agency": "ARL", "value": "stu_us_cat*0001"}])

    def test_elvira_book_block_and_identifiers(self):
        record = self.record
        # elvira_book.isbn first (ISBN-10 → ISBN-13), then otherId ISBNs; the DOI is bare.
        self.assertEqual(record.identifiers["isbn"], normalize_isbn("80-227-1234-5"))
        self.assertEqual(record.other_isbns, ["9788022750004"])
        self.assertEqual(record.identifiers["doi"], "10.1234/fluids.2024")
        self.assertEqual(record.edition, "2. vydanie")
        self.assertEqual((record.series, record.series_number), ("Edícia učebníc", "12"))
        self.assertEqual(record.page_count, 320)
        self.assertEqual(record.extra["lending"], "open")
        udc = [s.term for s in record.subjects if s.scheme == UDC_SCHEME]
        keywords = [s.term for s in record.subjects if s.scheme is None]
        self.assertEqual(udc, ["532", "621.22"])
        self.assertEqual(keywords, ["hydraulika", "prúdenie"])

    def test_values(self):
        values = record_to_values(self.record)
        self.assertEqual(values["authors"], ["Novák, Ján", "Kováčová, Eva"])
        self.assertEqual(values["language"], "sk")
        self.assertEqual(values["udc"], ["532", "621.22"])
        self.assertEqual(values["keywords"], ["hydraulika", "prúdenie"])
        self.assertEqual(values["published"], "2024-03")

    def test_legacy_extract_metadata_reads_native_shapes(self):
        metadata = extract_metadata(fixtures.data("version_book_published"), [], global_id=PID)
        self.assertEqual(metadata["title"], "Mechanika tekutín")
        self.assertEqual(metadata["authors"][0], {"name": "Ján", "surname": "Novák"})
        self.assertEqual(metadata["publisher"], "Spektrum STU")
        self.assertEqual(repr(metadata["published_at"]), "2024-03")
        self.assertEqual(metadata["language"].alpha2, "sk")


class OutboundPayloadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Language.objects.create(name="Slovak", alpha2="sk", alpha3="slk")

    def setUp(self):
        self.remote = dataset_to_source_record(fixtures.data("version_book_published"))

    def test_edit_payload_sets_changed_fields_only(self):
        values = record_to_values(self.remote)
        values["title"] = "Mechanika tekutín II"
        values["udc"] = ["532"]
        to_set, to_clear = edit_payload(values, ["title", "udc"], self.remote)
        self.assertEqual(to_clear, [])
        self.assertEqual(
            to_set,
            [{"typeName": "title", "value": "Mechanika tekutín II"}, {"typeName": "udc", "value": ["532"]}],
        )

    def test_edit_payload_compound_and_kept_remote_values(self):
        values = record_to_values(self.remote)
        values["authors"] = ["Novák, Ján"]
        values["isbn"] = "9788022750004"
        to_set, _ = edit_payload(values, ["authors", "isbn"], self.remote)
        by_name = {field["typeName"]: field["value"] for field in to_set}
        self.assertEqual(by_name["author"], [{"authorName": {"typeName": "authorName", "value": "Novák, Ján"}}])
        # The other ISBN Dataverse holds and the ARL otherId stay.
        self.assertEqual(by_name["isbn"], ["9788022750004"])
        agencies = [(item["otherIdAgency"]["value"], item["otherIdValue"]["value"]) for item in by_name["otherId"]]
        self.assertEqual(
            agencies,
            [("ISBN", "9788022750004"), ("DOI", "10.1234/fluids.2024"), ("ARL", "stu_us_cat*0001")],
        )

    def test_emptied_field_goes_to_delete_metadata_with_current_value(self):
        values = record_to_values(self.remote)
        values["edition"] = None
        to_set, to_clear = edit_payload(values, ["edition"], self.remote)
        self.assertEqual(to_set, [])
        self.assertEqual(to_clear, [{"typeName": "edition", "value": "2. vydanie"}])

    def test_owned_fields(self):
        values = record_to_values(self.remote)
        values.update({"elvira_entry_id": "e-1", "elvira_url": "https://portal/library?entry-detail-id=e-1"})
        to_set, _ = edit_payload(values, ["elvira_entry_id", "elvira_url"], self.remote)
        by_name = {field["typeName"]: field["value"] for field in to_set}
        self.assertEqual(by_name["elviraEntryId"], "e-1")
        self.assertEqual(
            by_name["relatedMaterial"], ["https://portal/library?entry-detail-id=e-1", "https://example.org/errata"]
        )

    def test_create_payload_is_a_book_with_required_citation_fields(self):
        payload = create_payload(self.remote, contact_email="lib@example.org", contact_name="Knižnica")
        self.assertEqual(payload["datasetType"], "book")
        version = payload["datasetVersion"]
        self.assertEqual(
            version["license"], {"name": "CC BY 4.0", "uri": "http://creativecommons.org/licenses/by/4.0"}
        )
        citation = {f["typeName"]: f for f in version["metadataBlocks"]["citation"]["fields"]}
        book = {f["typeName"]: f for f in version["metadataBlocks"]["elvira_book"]["fields"]}
        for name in ("title", "author", "datasetContact", "dsDescription", "subject"):
            self.assertIn(name, citation)
        self.assertEqual(
            citation["subject"],
            {"typeName": "subject", "value": ["Other"], "multiple": True, "typeClass": "controlledVocabulary"},
        )
        self.assertEqual(citation["language"]["value"], ["Slovak"])
        self.assertEqual(
            citation["author"]["value"][0]["authorName"],
            {"typeName": "authorName", "multiple": False, "typeClass": "primitive", "value": "Novák, Ján"},
        )
        self.assertEqual(book["udc"]["value"], ["532", "621.22"])
        self.assertEqual(book["pageCount"]["value"], "320")

    def test_round_trip_keeps_every_value(self):
        """record → create payload → served back → record: the same values (nothing lossy flips on re-read)."""
        payload = create_payload(self.remote, contact_email="lib@example.org", contact_name="Knižnica")
        again = dataset_to_source_record(_version_from_create(payload))
        self.assertEqual(record_to_values(again), record_to_values(self.remote))


@override_settings(EVILFLOWERS_PORTAL_URL="https://portal.example")
class EntryToRecordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source, _ = AuthSource.objects.get_or_create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User.objects.create(username="lib", name="Lib", surname="Rarian", auth_source=source)
        cls.catalog = Catalog.objects.create(creator=cls.user, title="STU", url_name="stu", is_public=True)
        cls.sk = Language.objects.create(name="Slovak", alpha2="sk", alpha3="slk")

    def test_entry_round_trips_through_a_dataset(self):
        entry = Entry.objects.create(
            creator=self.user,
            catalog=self.catalog,
            title="Mechanika tekutín",
            summary="Učebnica.",
            publisher="Spektrum STU",
            published_at="2024-03",
            language=self.sk,
            edition="2. vydanie",
            page_count=320,
            identifiers={"isbn": "80-227-1234-5", "doi": "10.1234/x"},
        )
        for position, (surname, name) in enumerate([("Novák", "Ján"), ("Kováčová", "Eva")]):
            author = Author.objects.create(catalog=self.catalog, surname=surname, name=name)
            EntryAuthor.objects.create(entry=entry, author=author, position=position)
        entry.categories.add(
            Category.objects.create(creator=self.user, catalog=self.catalog, term="532", scheme=UDC_SCHEME),
            Category.objects.create(creator=self.user, catalog=self.catalog, term="hydraulika"),
        )

        record = entry_to_source_record(entry)
        values = record_to_values(record)
        self.assertEqual(values["isbn"], normalize_isbn("80-227-1234-5"))
        self.assertEqual(values["elvira_entry_id"], str(entry.pk))
        self.assertEqual(values["elvira_url"], f"https://portal.example/library?entry-detail-id={entry.pk}")
        self.assertEqual(values["lending"], "open")

        payload = create_payload(record, contact_email="lib@example.org", contact_name="STU")
        served = dataset_to_source_record(_version_from_create(payload))
        self.assertEqual(record_to_values(served), values)
