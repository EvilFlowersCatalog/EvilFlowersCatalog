"""OAI-PMH paging and MARC/DC mapping (IP-034), against recorded pages; HTTP is a fake session."""

from django.test import SimpleTestCase
from pymarc import Field, Indicators, Record, Subfield

from apps.harvest.adapters.base import HarvestError, parse_xml
from apps.harvest.adapters.marc_file import read_marc
from apps.harvest.adapters.oai import OaiPmhAdapter
from apps.harvest.records import UDC_SCHEME
from apps.harvest.tests.base import ARL_PAGES, FakeSession, fixture


class Source:
    """The attributes an adapter reads, without a database row."""

    def __init__(self, **kwargs):
        self.slug = "arl-stu"
        self.base_url = "https://kis.example.org/oai"
        self.metadata_prefix = "marc21"
        self.set_spec = None
        self.cursor = None
        self.upload_path = None
        self.__dict__.update(kwargs)


class OaiPmhAdapterTests(SimpleTestCase):
    def harvest(self, pages=ARL_PAGES, **source):
        session = FakeSession(pages)
        adapter = OaiPmhAdapter(Source(**source), session=session)
        return adapter, session, list(adapter.records())

    def test_identify_first_then_list_records_following_the_resumption_token(self):
        adapter, session, records = self.harvest()

        self.assertEqual([call.get("verb") for call in session.calls], ["Identify", "ListRecords", "ListRecords"])
        self.assertEqual(session.calls[1], {"verb": "ListRecords", "metadataPrefix": "marc21"})
        # A resumption request carries the token only (OAI-PMH §3.5).
        self.assertEqual(session.calls[2], {"verb": "ListRecords", "resumptionToken": "page-2"})
        self.assertEqual(
            [record.record_id for record in records], ["0000101", "0000102", "0000103", records[3].record_id]
        )
        # The cursor is the first response's date, and only after the last page.
        self.assertEqual(adapter.cursor, "2026-11-16T10:00:05Z")
        self.assertIsNone(adapter.identify_info["adminEmail"])

    def test_incremental_harvest_sends_the_cursor_as_from_and_the_set(self):
        _adapter, session, _records = self.harvest(cursor="2026-11-01T00:00:00Z", set_spec="STU_US_CAT")
        self.assertEqual(session.calls[1]["from"], "2026-11-01T00:00:00Z")
        self.assertEqual(session.calls[1]["set"], "STU_US_CAT")

    def test_full_harvest_sends_no_from(self):
        session = FakeSession(ARL_PAGES)
        adapter = OaiPmhAdapter(Source(cursor="2026-11-01T00:00:00Z"), full=True, session=session)
        list(adapter.records())
        self.assertNotIn("from", session.calls[1])
        self.assertTrue(adapter.exhaustive)

    def test_day_granularity_cuts_the_cursor(self):
        session = FakeSession(ARL_PAGES)
        adapter = OaiPmhAdapter(Source(cursor="2026-11-01T12:30:00Z"), session=session)
        adapter.identify_info = {"granularity": "YYYY-MM-DD"}
        self.assertEqual(adapter._from(), "2026-11-01")

    def test_no_records_match_is_an_empty_harvest(self):
        adapter, _session, records = self.harvest({"Identify": "identify.xml", "ListRecords": "no_records.xml"})
        self.assertEqual(records, [])
        self.assertEqual(adapter.cursor, "2026-11-16T12:00:00Z")

    def test_http_error_is_a_harvest_error(self):
        with self.assertRaises(HarvestError):
            self.harvest({"Identify": "identify.xml"})

    def test_deleted_header_keeps_the_oai_identifier(self):
        _adapter, _session, records = self.harvest()
        deleted = records[3]
        self.assertTrue(deleted.deleted)
        self.assertEqual(deleted.extra["oai_identifier"], "oai:kis.example.org:STU_US_CAT*0000099")
        self.assertEqual(records[0].extra["oai_identifier"], "oai:kis.example.org:STU_US_CAT*0000101")

    def test_marc21_mapping(self):
        _adapter, _session, records = self.harvest()
        record = records[0]

        self.assertEqual(record.record_id, "0000101")
        self.assertEqual(record.title, "Numerická matematika")
        self.assertEqual(record.subtitle, "vybrané kapitoly")
        self.assertEqual(record.full_title, "Numerická matematika: vybrané kapitoly")
        # 020 $a with a qualifier, ISBN-10 → ISBN-13; the second 020 is another format.
        self.assertEqual(
            record.identifiers,
            {"isbn": "9788022712347", "issn": "1335-2016", "ccnb": "cnb000123456"},
        )
        self.assertEqual(record.other_isbns, ["9788022740005"])
        self.assertEqual(record.language, "sk")  # MARC `slo` (ISO 639-2/B)
        self.assertEqual(
            [(a.surname, a.name) for a in record.authors], [("Macák", "Marek"), ("Minarechová", "Zuzana")]
        )
        self.assertEqual(record.edition, "2. vydanie")
        self.assertEqual(record.publisher, "Spektrum STU")
        self.assertEqual(record.published, "2018")
        self.assertEqual(record.page_count, 245)
        self.assertEqual((record.series, record.series_number), ("Edícia vysokoškolských učebníc", "12"))
        self.assertEqual(record.summary, "Učebnica numerických metód")
        self.assertEqual(
            [(s.scheme, s.term) for s in record.subjects],
            [(UDC_SCHEME, "519.6"), ("arl-stu", "numerická matematika"), ("arl-stu", "učebnice")],
        )
        self.assertEqual(record.links, ["https://files.example.org/stu/0000101.pdf"])
        self.assertEqual(record.datestamp, "2026-11-10T08:00:00Z")

        second = records[1]
        self.assertEqual(second.identifiers, {"isbn": "9788022750783", "doi": "10.1234/abc-102"})
        self.assertEqual(
            (second.title, second.publisher, second.published), ("Applied mechanics", "Example Press", "2020")
        )
        self.assertEqual(second.language, "en")

    def test_oai_dc_mapping(self):
        _adapter, _session, records = self.harvest(
            {"Identify": "identify.xml", "ListRecords": "oai_dc.xml"}, metadata_prefix="oai_dc", slug="repo"
        )
        record = records[0]
        self.assertEqual(record.record_id, "oai:repo.example.org:42")
        self.assertEqual((record.title, record.subtitle), ("Dublin Core Book", "a subtitle"))
        self.assertEqual(record.identifiers, {"isbn": "9788022733335", "doi": "10.5555/xyz"})
        self.assertEqual(record.language, "en")
        self.assertEqual(record.published, "2019")
        self.assertEqual([(s.scheme, s.term) for s in record.subjects], [("repo", "Physics")])
        self.assertEqual(record.links, ["https://repo.example.org/record/42"])
        self.assertEqual(record.licence_url, "https://creativecommons.org/licenses/by/4.0/")

    def test_probe_reads_identify_formats_sets_and_one_sample(self):
        session = FakeSession(ARL_PAGES)
        probe = OaiPmhAdapter(Source(), session=session).probe()
        self.assertEqual(probe["identify"]["repositoryName"], "Example ARL catalogue")
        self.assertEqual([f["prefix"] for f in probe["metadata_formats"]], ["marc21", "oai_dc"])
        self.assertEqual(probe["sets"], [{"spec": "STU_US_CAT", "name": "STU union catalogue"}])
        self.assertEqual(probe["sample"]["record_id"], "0000101")
        self.assertEqual(probe["errors"], [])
        # One page only.
        self.assertEqual(sum(1 for call in session.calls if call.get("verb") == "ListRecords"), 1)


class HardenedXmlTests(SimpleTestCase):
    def test_external_entities_are_not_resolved(self):
        content = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>' b"<r>&x;</r>"
        try:
            root = parse_xml(content)
        except HarvestError:
            return
        self.assertNotIn("root:", "".join(root.itertext()))

    def test_garbage_is_a_harvest_error(self):
        with self.assertRaises(HarvestError):
            parse_xml(b"<not-closed>")


class MarcFileTests(SimpleTestCase):
    def test_marcxml_file(self):
        records = list(read_marc(fixture("delivery.xml"), subject_scheme="partner"))
        self.assertEqual([r.record_id for r in records], ["P-1", "9788022733335"])  # no 001: the ISBN
        self.assertEqual(records[0].language, "cs")  # `cze`

    def test_iso2709_file(self):
        record = Record()
        record.add_field(Field(tag="001", data="ISO-1"))
        record.add_field(Field(tag="020", indicators=Indicators(" ", " "), subfields=[Subfield("a", "8022750786")]))
        record.add_field(
            Field(tag="245", indicators=Indicators("1", "0"), subfields=[Subfield("a", "Binárna kniha /")])
        )
        record.add_field(Field(tag="041", indicators=Indicators("0", " "), subfields=[Subfield("a", "slo")]))
        record.leader = record.leader[:9] + "a" + record.leader[10:]

        records = list(read_marc(record.as_marc(), subject_scheme="partner"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_id, "ISO-1")
        self.assertEqual(records[0].title, "Binárna kniha")
        self.assertEqual(records[0].identifiers["isbn"], "9788022750783")
