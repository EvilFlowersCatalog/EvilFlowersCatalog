"""IP-033 Phase 2: the six verbs, every error code of §3.6, paging and tokens; every response is validated
against the vendored OAI-PMH.xsd (with oai_dc.xsd, MARC21slim.xsd and oai-identifier.xsd)."""

from datetime import datetime, timedelta, timezone

from django.core import signing
from django.test import override_settings
from lxml import etree

from apps.core.models import Catalog, Category, Entry
from apps.oai.models import OaiRecord
from apps.oai.tests.base import NS, OaiTestCase, schema

ID = "oai:evilflowers.elvira.stuba.sk:"


def at(second: int, day: int = 1) -> datetime:
    return datetime(2026, 1, day, 0, 0, second, tzinfo=timezone.utc)


class ProtocolTestCase(OaiTestCase):
    def oai(self, method="get", **params) -> etree._Element:
        query = params.pop("_query", None)
        if method == "post":
            response = self.client.post("/oai", params)
        else:
            response = self.client.get(f"/oai?{query}" if query else "/oai", params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/xml; charset=utf-8")
        root = etree.fromstring(response.content)
        valid = schema().validate(root)
        self.assertTrue(valid, f"{schema().error_log}\n{response.content.decode()}")
        return root

    def error(self, root) -> str:
        errors = root.findall("oai:error", NS)
        self.assertEqual(len(errors), 1, etree.tostring(root))
        return errors[0].get("code")

    def request_attributes(self, root) -> dict:
        return dict(root.find("oai:request", NS).attrib)

    def books(self, count: int, catalog=None) -> list[Entry]:
        books = [self.entry(title=f"Kniha {index}", catalog=catalog) for index in range(count)]
        self.sync(*books)
        for index, book in enumerate(books):
            self.backdate(book, at(index))
        return books


class IdentifyTests(ProtocolTestCase):
    def test_identify(self):
        books = self.books(2)
        root = self.oai(verb="Identify")
        identify = root.find("oai:Identify", NS)
        self.assertEqual(identify.findtext("oai:baseURL", namespaces=NS), "https://evilflowers.elvira.stuba.sk/oai")
        self.assertEqual(identify.findtext("oai:protocolVersion", namespaces=NS), "2.0")
        self.assertEqual(identify.findtext("oai:adminEmail", namespaces=NS), "elvira@stuba.sk")
        self.assertEqual(identify.findtext("oai:deletedRecord", namespaces=NS), "persistent")
        self.assertEqual(identify.findtext("oai:granularity", namespaces=NS), "YYYY-MM-DDThh:mm:ssZ")
        self.assertEqual(identify.findtext("oai:earliestDatestamp", namespaces=NS), "2026-01-01T00:00:00Z")
        self.assertEqual(
            identify.findtext("oai:description/id:oai-identifier/id:repositoryIdentifier", namespaces=NS),
            "evilflowers.elvira.stuba.sk",
        )
        self.assertEqual(self.request_attributes(root), {"verb": "Identify"})
        self.assertTrue(books)

    def test_identify_of_an_empty_repository_is_valid(self):
        root = self.oai(verb="Identify")
        self.assertIsNotNone(root.find("oai:Identify/oai:earliestDatestamp", NS).text)

    def test_post_form_is_accepted(self):
        root = self.oai(method="post", verb="Identify")
        self.assertIsNotNone(root.find("oai:Identify", NS))

    @override_settings(EVILFLOWERS_OAI_ENABLED=False)
    def test_disabled_answers_404(self):
        self.assertEqual(self.client.get("/oai", {"verb": "Identify"}).status_code, 404)


class GrammarTests(ProtocolTestCase):
    def assertBad(self, code, **params):
        root = self.oai(**params)
        self.assertEqual(self.error(root), code)
        # §3.2: no attributes after badVerb or badArgument.
        self.assertEqual(self.request_attributes(root), {})
        self.assertEqual(root.findtext("oai:request", namespaces=NS), "https://evilflowers.elvira.stuba.sk/oai")

    def test_bad_verbs(self):
        self.assertBad("badVerb")
        self.assertBad("badVerb", verb="ListEverything")
        self.assertBad("badVerb", _query="verb=Identify&verb=Identify")
        self.assertBad("badVerb", method="post", verb="Nope")

    def test_bad_arguments(self):
        self.assertBad("badArgument", verb="Identify", set="stu")
        self.assertBad("badArgument", verb="ListRecords")
        self.assertBad("badArgument", verb="GetRecord", metadataPrefix="oai_dc")
        self.assertBad("badArgument", verb="ListRecords", metadataPrefix="oai_dc", access_token="x")
        self.assertBad("badArgument", _query="verb=ListRecords&metadataPrefix=oai_dc&metadataPrefix=marc21")
        self.assertBad("badArgument", verb="ListRecords", metadataPrefix="oai dc")
        self.assertBad("badArgument", verb="ListRecords", metadataPrefix="oai_dc", set="stu/x")
        self.assertBad("badArgument", verb="ListSets", resumptionToken="x", set="stu")
        self.assertBad("badArgument", verb="ListRecords", metadataPrefix="oai_dc", resumptionToken="x")

    def test_bad_dates(self):
        prefix = {"verb": "ListIdentifiers", "metadataPrefix": "oai_dc"}
        self.assertBad("badArgument", **prefix, **{"from": "2026-02-01", "until": "2026-01-01"})
        self.assertBad("badArgument", **prefix, **{"from": "2026-01-01", "until": "2026-02-01T00:00:00Z"})
        self.assertBad("badArgument", **prefix, **{"from": "2026-01-01T00:00:00.5Z"})
        self.assertBad("badArgument", **prefix, **{"from": "2026-01-01T00:00Z"})
        self.assertBad("badArgument", **prefix, **{"until": "2026-13-01"})
        self.assertBad("badArgument", **prefix, **{"from": "yesterday"})

    def test_hostile_input_never_breaks_the_document(self):
        # Control characters are no XML; identifiers that are no xs:anyURI could not be echoed validly.
        self.assertBad("badVerb", _query="verb=%00")
        self.assertBad("badArgument", _query="verb=Identify&a%0Bb=1")
        self.assertBad("badArgument", _query="verb=ListRecords&metadataPrefix=oai_dc&from=%0B")
        self.assertBad("badArgument", _query="verb=GetRecord&metadataPrefix=oai_dc&identifier=a%0Bb")
        self.assertBad("badArgument", _query="verb=ListRecords&resumptionToken=%01")
        self.assertBad("badArgument", _query="verb=GetRecord&metadataPrefix=oai_dc&identifier=1:2")
        self.assertBad("badArgument", _query="verb=GetRecord&metadataPrefix=oai_dc&identifier=100%25")
        self.assertBad("badArgument", _query="verb=ListMetadataFormats&identifier=")
        root = self.oai(_query="verb=GetRecord&metadataPrefix=oai_dc&identifier=%C3%A4%3Cb")
        self.assertEqual(self.error(root), "idDoesNotExist")


class MetadataFormatTests(ProtocolTestCase):
    def test_formats_of_the_repository_and_of_a_record(self):
        book = self.books(1)[0]
        for params in ({}, {"identifier": f"{ID}{book.pk}"}):
            root = self.oai(verb="ListMetadataFormats", **params)
            prefixes = root.xpath("//oai:metadataPrefix/text()", namespaces=NS)
            self.assertEqual(prefixes, ["oai_dc", "marc21"])
        namespaces = root.xpath("//oai:metadataNamespace/text()", namespaces=NS)
        self.assertIn("http://www.loc.gov/MARC21/slim", namespaces)

    def test_unknown_identifier(self):
        for identifier in (f"{ID}00000000-0000-0000-0000-000000000000", "oai:other.example.org:1", "nonsense"):
            root = self.oai(verb="ListMetadataFormats", identifier=identifier)
            self.assertEqual(self.error(root), "idDoesNotExist")
            self.assertEqual(self.request_attributes(root)["identifier"], identifier)


class SetTests(ProtocolTestCase):
    def test_catalog_and_category_sets(self):
        book = self.entry()
        book.categories.add(self.udc)
        self.sync(book)
        root = self.oai(verb="ListSets")
        sets = {
            item.findtext("oai:setSpec", namespaces=NS): item.findtext("oai:setName", namespaces=NS)
            for item in root.findall("oai:ListSets/oai:set", NS)
        }
        self.assertEqual(sets, {"stu": "STU", f"stu:{self.udc.pk}": "Elektrotechnika (UDC 621.3)"})
        self.assertIsNone(root.find("oai:ListSets/oai:resumptionToken", NS))

    @override_settings(EVILFLOWERS_OAI_PAGE_SIZE=1)
    def test_sets_are_paged(self):
        Catalog.objects.create(creator=self.owner, title="FIIT", url_name="fiit", harvestable=True)
        root = self.oai(verb="ListSets")
        self.assertEqual(root.xpath("//oai:setSpec/text()", namespaces=NS), ["fiit"])
        token = root.find("oai:ListSets/oai:resumptionToken", NS)
        self.assertEqual((token.get("completeListSize"), token.get("cursor")), ("2", "0"))

        root = self.oai(verb="ListSets", resumptionToken=token.text)
        self.assertEqual(root.xpath("//oai:setSpec/text()", namespaces=NS), ["stu"])
        final = root.find("oai:ListSets/oai:resumptionToken", NS)
        self.assertEqual((final.text, final.get("cursor")), (None, "1"))

    def test_no_harvestable_catalog_means_no_set_hierarchy(self):
        Catalog.objects.filter(harvestable=True).update(harvestable=False)
        self.assertEqual(self.error(self.oai(verb="ListSets")), "noSetHierarchy")


class GetRecordTests(ProtocolTestCase):
    def test_get_record_in_both_formats(self):
        book = self.books(1)[0]
        for prefix, path in (("oai_dc", "oai_dc:dc/dc:title"), ("marc21", "marc:record/marc:leader")):
            root = self.oai(verb="GetRecord", identifier=f"{ID}{book.pk}", metadataPrefix=prefix)
            record = root.find("oai:GetRecord/oai:record", NS)
            self.assertEqual(record.findtext("oai:header/oai:identifier", namespaces=NS), f"{ID}{book.pk}")
            self.assertEqual(record.findtext("oai:header/oai:datestamp", namespaces=NS), "2026-01-01T00:00:00Z")
            self.assertEqual(record.xpath("oai:header/oai:setSpec/text()", namespaces=NS), ["stu"])
            self.assertIsNotNone(record.find(f"oai:metadata/{path}", NS))

    def test_a_deleted_record_is_a_header_only(self):
        book = self.books(1)[0]
        entry_id = book.pk
        book.delete()
        self.sync(entry_id)
        root = self.oai(verb="GetRecord", identifier=f"{ID}{entry_id}", metadataPrefix="oai_dc")
        record = root.find("oai:GetRecord/oai:record", NS)
        self.assertEqual(record.find("oai:header", NS).get("status"), "deleted")
        self.assertIsNone(record.find("oai:metadata", NS))

    def test_errors_echo_the_request(self):
        book = self.books(1)[0]
        root = self.oai(verb="GetRecord", identifier=f"{ID}{book.pk}", metadataPrefix="mods")
        self.assertEqual(self.error(root), "cannotDisseminateFormat")
        self.assertEqual(
            self.request_attributes(root),
            {"verb": "GetRecord", "identifier": f"{ID}{book.pk}", "metadataPrefix": "mods"},
        )
        root = self.oai(verb="GetRecord", identifier=f"{ID}{Category.objects.first().pk}", metadataPrefix="oai_dc")
        self.assertEqual(self.error(root), "idDoesNotExist")


class ListTests(ProtocolTestCase):
    def identifiers(self, root, verb="ListIdentifiers") -> list[str]:
        path = (
            f"oai:{verb}/oai:header/oai:identifier"
            if verb == "ListIdentifiers"
            else (f"oai:{verb}/oai:record/oai:header/oai:identifier")
        )
        return [element.text.rsplit(":", 1)[1] for element in root.findall(path, NS)]

    def test_day_and_seconds_granularity(self):
        books = self.books(3)
        self.backdate(books[2], at(0, day=2))
        ids = [str(book.pk) for book in books]

        root = self.oai(
            verb="ListIdentifiers", metadataPrefix="oai_dc", **{"from": "2026-01-01", "until": "2026-01-01"}
        )
        self.assertEqual(self.identifiers(root), ids[:2])
        root = self.oai(
            verb="ListIdentifiers",
            metadataPrefix="oai_dc",
            **{"from": "2026-01-01T00:00:01Z", "until": "2026-01-02T00:00:00Z"},
        )
        self.assertEqual(self.identifiers(root), ids[1:])
        root = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc", **{"from": "2026-01-02"})
        self.assertEqual(self.identifiers(root), ids[2:])
        root = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc", until="2026-01-01T00:00:00Z")
        self.assertEqual(self.identifiers(root), ids[:1])

    def test_no_records_match_and_unknown_formats(self):
        self.books(1)
        root = self.oai(verb="ListRecords", metadataPrefix="oai_dc", **{"from": "2030-01-01"})
        self.assertEqual(self.error(root), "noRecordsMatch")
        self.assertEqual(self.request_attributes(root)["from"], "2030-01-01")
        self.assertEqual(
            self.error(self.oai(verb="ListRecords", metadataPrefix="oai_dc", set="nope")), "noRecordsMatch"
        )
        self.assertEqual(self.error(self.oai(verb="ListRecords", metadataPrefix="mods")), "cannotDisseminateFormat")

    def test_sets_select_records(self):
        fiit = Catalog.objects.create(creator=self.owner, title="FIIT", url_name="fiit", harvestable=True)
        stu_book = self.books(1)[0]
        stu_book.categories.add(self.udc)
        self.sync(stu_book)
        fiit_book = self.entry(catalog=fiit)
        self.sync(fiit_book)
        root = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc", set="fiit")
        self.assertEqual(self.identifiers(root), [str(fiit_book.pk)])
        root = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc", set=f"stu:{self.udc.pk}")
        self.assertEqual(self.identifiers(root), [str(stu_book.pk)])
        root = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc", set="stu")
        self.assertEqual(self.identifiers(root), [str(stu_book.pk)])

    def test_deleted_records_appear_as_deleted_headers(self):
        books = self.books(2)
        entry_id = books[0].pk
        books[0].delete()
        self.sync(entry_id)
        root = self.oai(verb="ListRecords", metadataPrefix="marc21")
        records = root.findall("oai:ListRecords/oai:record", NS)
        self.assertEqual(len(records), 2)
        deleted = [record for record in records if record.find("oai:header", NS).get("status") == "deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertIsNone(deleted[0].find("oai:metadata", NS))
        headers = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc").findall(
            "oai:ListIdentifiers/oai:header", NS
        )
        self.assertEqual(sorted(header.get("status") or "live" for header in headers), ["deleted", "live"])

    @override_settings(EVILFLOWERS_OAI_PAGE_SIZE=2)
    def test_paging_across_pages_with_a_change_mid_walk(self):
        books = self.books(5)
        ids = [str(book.pk) for book in books]

        first = self.oai(verb="ListRecords", metadataPrefix="oai_dc")
        self.assertEqual(self.identifiers(first, "ListRecords"), ids[:2])
        token = first.find("oai:ListRecords/oai:resumptionToken", NS)
        self.assertEqual((token.get("completeListSize"), token.get("cursor")), ("5", "0"))

        # Re-issuing a token returns the same page (§3.5.1).
        again = self.oai(verb="ListRecords", resumptionToken=token.text)
        self.assertEqual(self.identifiers(again, "ListRecords"), ids[2:4])

        # Book 3 changes during the walk: its new datestamp is past the frozen `until`, so this list skips it
        # and the next incremental harvest brings it.
        OaiRecord.objects.filter(entry_id=books[3].pk).update(
            datestamp=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        )
        second = self.oai(verb="ListRecords", resumptionToken=token.text)
        self.assertEqual(self.identifiers(second, "ListRecords"), [ids[2], ids[4]])
        final = second.find("oai:ListRecords/oai:resumptionToken", NS)
        self.assertEqual((final.text, final.get("cursor"), final.get("completeListSize")), (None, "2", "5"))

        # A later incremental harvest (from the old responseDate) brings the changed record.
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        later = self.oai(
            verb="ListIdentifiers", metadataPrefix="oai_dc", **{"from": "2026-01-02T00:00:00Z", "until": until}
        )
        self.assertEqual(self.identifiers(later), [ids[3]])

    @override_settings(EVILFLOWERS_OAI_PAGE_SIZE=2)
    def test_the_last_page_carries_an_empty_token(self):
        books = self.books(3)
        first = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc")
        token = first.find("oai:ListIdentifiers/oai:resumptionToken", NS)
        last = self.oai(verb="ListIdentifiers", resumptionToken=token.text)
        self.assertEqual(self.identifiers(last), [str(books[2].pk)])
        final = last.find("oai:ListIdentifiers/oai:resumptionToken", NS)
        self.assertEqual((final.text, final.get("completeListSize"), final.get("cursor")), (None, "3", "2"))
        self.assertEqual(self.request_attributes(last), {"verb": "ListIdentifiers", "resumptionToken": token.text})

    @override_settings(EVILFLOWERS_OAI_PAGE_SIZE=2)
    def test_tampered_and_foreign_tokens(self):
        self.books(3)
        token = self.oai(verb="ListRecords", metadataPrefix="oai_dc").find("oai:ListRecords/oai:resumptionToken", NS)
        tampered = token.text[:-2] + ("AA" if not token.text.endswith("AA") else "BB")
        for verb, value in (
            ("ListRecords", tampered),
            ("ListIdentifiers", token.text),
            ("ListRecords", signing.dumps({"v": "ListRecords"}, salt="other")),
            ("ListRecords", signing.dumps({"v": "ListRecords"}, salt="oai")),
        ):
            root = self.oai(verb=verb, resumptionToken=value)
            self.assertEqual(self.error(root), "badResumptionToken")
            self.assertEqual(self.request_attributes(root), {"verb": verb, "resumptionToken": value})

    def test_a_single_page_has_no_token(self):
        self.books(2)
        root = self.oai(verb="ListRecords", metadataPrefix="oai_dc")
        self.assertIsNone(root.find("oai:ListRecords/oai:resumptionToken", NS))
        self.assertEqual(len(root.findall("oai:ListRecords/oai:record", NS)), 2)

    def test_until_defaults_to_the_response_date(self):
        books = self.books(2)
        OaiRecord.objects.filter(entry_id=books[1].pk).update(datestamp=datetime(2100, 1, 1, tzinfo=timezone.utc))
        root = self.oai(verb="ListIdentifiers", metadataPrefix="oai_dc")
        self.assertEqual(self.identifiers(root), [str(books[0].pk)])
