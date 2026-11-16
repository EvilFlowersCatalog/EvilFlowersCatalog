"""IP-033 crosswalks: `oai_dc` and `marc21` content, schema-valid, from the stored projection only."""

from unittest.mock import patch

from apps.core.models import Category
from apps.oai.projection import LCP_ACCESS_STATEMENT
from apps.oai.tests.base import NS, PORTAL
from apps.oai.tests.test_protocol import ID, ProtocolTestCase, at


class CrosswalkTests(ProtocolTestCase):
    def setUp(self):
        super().setUp()
        self.book = self.entry(
            title="Elektrické stroje",
            authors=(("Novák", "Ján"), ("Kováč", "Peter")),
            identifiers={"isbn": "80-227-1234-5", "doi": "10.1234/abc", "arl_stu": "0076487"},
            publisher="Vydavateľstvo STU",
            published_at="2019",
            edition="2. vyd.",
            page_count=321,
            summary="Učebnica o strojoch.",
            rights={"license_url": "https://creativecommons.org/licenses/by/4.0/", "license_name": "CC BY 4.0"},
        )
        self.local = Category.objects.create(
            creator=self.owner, catalog=self.stu, term="strojarstvo", label={"sk": "Strojárstvo", "en": "Machinery"}
        )
        self.book.categories.add(self.udc, self.local)
        self.sync(self.book)
        self.backdate(self.book, at(5))

    def metadata(self, prefix: str):
        root = self.oai(verb="GetRecord", identifier=f"{ID}{self.book.pk}", metadataPrefix=prefix)
        return root.find("oai:GetRecord/oai:record/oai:metadata", NS)[0]

    def test_oai_dc(self):
        dc = self.metadata("oai_dc")

        def values(name):
            return dc.xpath(f"dc:{name}/text()", namespaces=NS)

        self.assertEqual(values("title"), ["Elektrické stroje"])
        self.assertEqual(values("creator"), ["Novák, Ján", "Kováč, Peter"])
        self.assertIn("UDC 621.3", values("subject"))
        self.assertIn("Elektrotechnika", values("subject"))
        self.assertIn("Machinery", values("subject"))
        self.assertEqual(dc.xpath("dc:subject[text()='Machinery']/@xml:lang", namespaces=NS), ["en"])
        self.assertEqual(values("description"), ["Učebnica o strojoch."])
        self.assertEqual(values("publisher"), ["Vydavateľstvo STU"])
        self.assertEqual(values("date"), ["2019"])
        self.assertEqual(values("type"), ["Text"])
        self.assertEqual(values("format"), ["application/pdf"])
        self.assertEqual(
            values("identifier"),
            [
                f"{PORTAL}/library?entry-detail-id={self.book.pk}",
                "urn:isbn:9788022712347",
                "https://doi.org/10.1234/abc",
            ],
        )
        self.assertEqual(values("language"), ["sk"])
        self.assertEqual(values("relation"), ["https://kis.cvt.stuba.sk/arl-stu/sk/detail-stu_us_cat-0076487/"])
        self.assertEqual(values("rights"), ["https://creativecommons.org/licenses/by/4.0/", "CC BY 4.0"])

    def marc(self):
        record = self.metadata("marc21")
        control = {field.get("tag"): field.text for field in record.findall("marc:controlfield", NS)}
        data = {}
        for field in record.findall("marc:datafield", NS):
            subfields = [(sub.get("code"), sub.text) for sub in field.findall("marc:subfield", NS)]
            data.setdefault(field.get("tag"), []).append((field.get("ind1") + field.get("ind2"), subfields))
        return record, control, data

    def test_marc21(self):
        record, control, data = self.marc()
        self.assertEqual(len(record.findtext("marc:leader", namespaces=NS)), 24)
        self.assertEqual(control["001"], str(self.book.pk))
        self.assertEqual(control["005"], "20260101000005.0")
        self.assertEqual(len(control["008"]), 40)
        self.assertEqual(control["008"][7:11], "2019")
        self.assertEqual(control["008"][35:38], "slk")
        self.assertEqual(data["020"], [("  ", [("a", "9788022712347")])])
        self.assertEqual(data["024"], [("7 ", [("a", "10.1234/abc"), ("2", "doi")])])
        self.assertEqual(
            data["035"],
            [("  ", [("a", f"(ELVIRA){self.book.pk}")]), ("  ", [("a", "(SK-BA-STU)0076487")])],
        )
        self.assertEqual(data["041"], [("  ", [("a", "slk")])])
        self.assertEqual(data["080"], [("  ", [("a", "621.3")])])
        self.assertEqual(data["100"], [("1 ", [("a", "Novák, Ján")])])
        self.assertEqual(data["245"], [("10", [("a", "Elektrické stroje")])])
        self.assertEqual(data["250"], [("  ", [("a", "2. vyd.")])])
        self.assertEqual(data["264"], [(" 1", [("b", "Vydavateľstvo STU"), ("c", "2019")])])
        self.assertEqual(data["300"], [("  ", [("a", "1 online resource (321 pages)")])])
        self.assertNotIn("506", data)
        self.assertEqual(data["520"], [("  ", [("a", "Učebnica o strojoch.")])])
        self.assertEqual(
            data["540"], [("  ", [("a", "CC BY 4.0"), ("u", "https://creativecommons.org/licenses/by/4.0/")])]
        )
        self.assertEqual(sorted(value[1][0][1] for value in data["653"]), ["Elektrotechnika", "Strojárstvo"])
        self.assertEqual(data["700"], [("1 ", [("a", "Kováč, Peter")])])
        self.assertEqual(
            data["856"],
            [("40", [("u", f"{PORTAL}/library?entry-detail-id={self.book.pk}"), ("y", "Požičať v Elvíre")])],
        )

    def test_lcp_titles_state_the_restriction(self):
        self.book.config = {"readium_enabled": True}
        self.book.save()
        from django.utils import timezone

        from apps.readium.models import EncryptedContent

        EncryptedContent.objects.create(
            acquisition=self.book.acquisitions.get(),
            lcp_content_id="lcp-1",
            encrypted_path="x.lcpdf",
            status=EncryptedContent.EncryptionStatus.REGISTERED,
            encrypted_at=timezone.now(),
        )
        self.sync(self.book)
        _record, _control, data = self.marc()
        self.assertEqual(data["506"], [("1 ", [("a", LCP_ACCESS_STATEMENT)])])
        dc = self.metadata("oai_dc")
        self.assertIn(LCP_ACCESS_STATEMENT, dc.xpath("dc:rights/text()", namespaces=NS))

    def test_no_portal_link_while_cite_base_url_is_empty(self):
        with patch("apps.oai.projection.settings_store.get", return_value=""):
            self.sync(self.book)
        _record, _control, data = self.marc()
        self.assertNotIn("856", data)
        identifiers = self.metadata("oai_dc").xpath("dc:identifier/text()", namespaces=NS)
        self.assertFalse([value for value in identifiers if value.startswith(PORTAL)])

    def test_a_minimal_record_is_valid(self):
        bare = self.entry(title="Bez autora", authors=(), language=None)
        self.sync(bare)
        root = self.oai(verb="GetRecord", identifier=f"{ID}{bare.pk}", metadataPrefix="marc21")
        record = root.find("oai:GetRecord/oai:record/oai:metadata", NS)[0]
        title = record.find("marc:datafield[@tag='245']", NS)
        self.assertEqual(title.get("ind1"), "0")
        control = {field.get("tag"): field.text for field in record.findall("marc:controlfield", NS)}
        self.assertEqual(control["008"][6:11], "nuuuu")
        self.assertEqual(control["008"][35:38], "und")
