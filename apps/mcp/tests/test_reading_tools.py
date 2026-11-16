"""#176: the MCP content tools read through the content gate and hand out addresses, not copies."""

import json
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.assistant.tests import fixtures as books
from apps.content import policies
from apps.content.models import ContentDisclosure, ContentPack
from apps.core.models import Catalog
from apps.extraction import locators
from apps.mcp import uris
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry
from apps.readium.models import License


class ReadingToolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.reader = books.user()
        cls.other_reader = books.user("other")
        cls.catalog = books.catalog(cls.reader)
        cls.hidden_catalog = books.catalog(cls.reader, public=False, name="hidden")
        cls.entry, cls.entry_run, cls.paragraphs, cls.venturi, _shear = books.publication(cls.reader, cls.catalog)
        cls.lcp, cls.lcp_run, cls.lcp_paragraphs, *_rest = books.publication(
            cls.reader, cls.catalog, title="Lent Fluids", readium=True
        )
        cls.secret, cls.secret_run, *_rest = books.publication(cls.reader, cls.hidden_catalog, title="Secret")
        for run in (cls.entry_run, cls.lcp_run, cls.secret_run):
            locators.assign(run)
        for paragraph in cls.paragraphs + cls.lcp_paragraphs:
            paragraph.refresh_from_db()
        cls.venturi.refresh_from_db()

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)
        self.server = McpServer(registry)

    # -- helpers ---------------------------------------------------------------------------------------------

    def call(self, name: str, arguments: dict, user=None) -> dict:
        request = RequestFactory().post("/mcp/v1")
        request.user = user or AnonymousUser()
        response = self.server.handle(
            request,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            LATEST_PROTOCOL_VERSION,
        )
        return response["result"]

    def ok(self, name: str, arguments: dict, user=None) -> dict:
        result = self.call(name, arguments, user)
        self.assertFalse(result.get("isError"), result["content"][0]["text"])
        return result["structuredContent"]

    def uri(self, entry, paragraph) -> str:
        return uris.locator_uri(entry.pk, paragraph.locator_key)

    # -- outline ---------------------------------------------------------------------------------------------

    def test_outline_shows_headings_and_regions_at_the_callers_tier(self):
        outline = self.ok("get_outline", {"entry_id": str(self.entry.pk)})
        self.assertEqual([section["heading"] for section in outline["sections"]], ["1 Introduction", "2 Bernoulli"])
        self.assertTrue(all(section["uri"].startswith("evilflowers://entry/") for section in outline["sections"]))
        venturi = next(region for region in outline["regions"] if region["page"] == 4)
        self.assertEqual((venturi["label"], venturi["caption"][:7]), ("Obr. 2.1", "Obr. 2."))
        self.assertEqual(outline["access"]["tier"], "full")

        lent = self.ok("get_outline", {"entry_id": str(self.lcp.pk)})
        self.assertEqual({section["heading"] for section in lent["sections"]}, {None})
        self.assertNotIn("caption", lent["regions"][0])
        self.assertEqual(lent["access"]["tier"], "metadata")
        self.assertTrue(lent["access"]["how_to_unlock"][0].startswith("borrow: "))

    def test_a_hidden_entry_is_not_found_by_any_tool(self):
        hidden = str(self.secret.pk)
        for name, arguments in (
            ("get_outline", {"entry_id": hidden}),
            ("my_access", {"entry_id": hidden}),
            ("read", {"uri": uris.entry_uri(hidden)}),
            ("resolve_citation", {"uri": uris.locator_uri(hidden, self.secret_run.paragraphs.first().locator_key)}),
        ):
            with self.subTest(tool=name):
                result = self.call(name, arguments)
                self.assertTrue(result["isError"])
                self.assertIn("does not exist", result["content"][0]["text"].lower())

    # -- read ------------------------------------------------------------------------------------------------

    def test_read_returns_whole_paragraphs_with_addresses_and_region_links(self):
        # The Venturi paragraph is on page 4, with the figure "Obr. 2.1".
        start = self.paragraphs[3]
        result = self.call("read", {"uri": self.uri(self.entry, start), "context": 1, "max_chars": 200})
        payload = result["structuredContent"]
        texts = [paragraph["text"] for paragraph in payload["paragraphs"]]
        self.assertEqual(texts[0], self.paragraphs[2].text)
        self.assertIn(start.text, texts)
        self.assertTrue(
            all(paragraph["url"].endswith(paragraph["uri"].rsplit("/", 1)[-1]) for paragraph in payload["paragraphs"])
        )
        self.assertTrue(payload["paragraphs"][0]["sentences"])
        self.assertIsNotNone(payload["next_cursor"])
        links = [block for block in result["content"] if block["type"] == "resource_link"]
        venturi = uris.locator_uri(self.entry.pk, self.venturi.locator_key)
        self.assertIn(
            {"type": "resource_link", "uri": venturi, "name": "Obr. 2.1", "mimeType": "application/json"}, links
        )
        self.assertEqual(payload["access"]["policy"], "parity@1")

        more = self.ok("read", {"uri": self.uri(self.entry, start), "cursor": payload["next_cursor"]})
        self.assertNotIn(payload["paragraphs"][0]["text"], [paragraph["text"] for paragraph in more["paragraphs"]])

    def test_an_lcp_book_under_parity_gives_metadata_and_borrow_but_no_text(self):
        result = self.ok("read", {"uri": self.uri(self.lcp, self.lcp_paragraphs[0])}, self.reader)
        self.assertEqual(result["paragraphs"], [])
        self.assertIn("Readium LCP", result["refused"])
        self.assertEqual(result["access"]["tier"], "metadata")
        self.assertTrue(result["access"]["how_to_unlock"][0].startswith("borrow: "))

    def test_reads_are_logged_for_machine_callers(self):
        self.ok("read", {"uri": self.uri(self.entry, self.paragraphs[0])}, self.reader)
        rows = ContentDisclosure.objects.filter(entry=self.entry, surface="mcp")
        self.assertTrue(rows.exists())
        self.assertEqual(rows.first().policy, "parity@1")

    # -- presets ---------------------------------------------------------------------------------------------

    def test_tiers_per_preset(self):
        lent = self.uri(self.lcp, self.lcp_paragraphs[0])
        open_book = self.uri(self.entry, self.paragraphs[0])
        expected = {
            # policy: (tier of the LCP book without a loan, its text readable, the open book's tier)
            "parity": ("metadata", False, "full"),
            "open-access": ("full", True, "full"),
            "licensed-standard": ("metadata", False, "full"),
            "licensed-strict": ("metadata", False, "full"),
            "campus-only": ("metadata", False, "full"),
        }
        for key, (tier, readable, open_tier) in expected.items():
            with self.subTest(policy=key):
                Catalog.objects.filter(pk=self.catalog.pk).update(content_policy=key)
                result = self.ok("read", {"uri": lent}, self.reader)
                self.assertEqual((result["access"]["tier"], bool(result["paragraphs"])), (tier, readable))
                self.assertEqual(result["access"]["policy"], f"{key}@1")
                self.assertEqual(self.ok("read", {"uri": open_book}, self.reader)["access"]["tier"], open_tier)

    def test_a_past_loan_reads_within_its_budget(self):
        uri = self.uri(self.lcp, self.lcp_paragraphs[0])
        Catalog.objects.filter(pk=self.catalog.pk).update(content_policy="licensed-standard")
        now = timezone.now()
        License.objects.create(
            entry=self.lcp,
            user=self.reader,
            state=License.LicenseState.RETURNED,
            starts_at=now - timedelta(days=9),
            expires_at=now - timedelta(days=2),
            device_count=1,
        )
        past = self.ok("read", {"uri": uri, "max_chars": 2000}, self.reader)
        self.assertEqual((past["access"]["basis"], past["access"]["tier"]), ("past_loan", "passage"))
        # 10 % of a ~430-character book is ~43 characters: the first paragraph does not fit, so nothing is returned,
        # nothing is charged, and the paragraph is listed as left out (never cut).
        self.assertEqual(past["paragraphs"], [])
        self.assertGreater(past["left_out"][0]["chars"], past["access"]["budget_left"])
        self.assertEqual(past["access"]["chars_charged"], 0)
        self.assertFalse(ContentDisclosure.objects.filter(entry=self.lcp, chars_charged__gt=0).exists())
        self.assertEqual(self.ok("read", {"uri": uri}, self.other_reader)["paragraphs"], [])

    # -- citations -------------------------------------------------------------------------------------------

    def test_a_citation_is_an_address_that_resolves_at_the_callers_tier(self):
        uri = self.uri(self.entry, self.paragraphs[2])
        citation = self.ok("resolve_citation", {"uri": uri})
        self.assertEqual((citation["uri"], citation["resolution"], citation["kind"]), (uri, "exact", "paragraph"))
        self.assertTrue(citation["url"].endswith(f"/cite/{self.entry.pk}/{self.paragraphs[2].locator_key}"))
        self.assertEqual(citation["work"]["title"], "Fluid Mechanics")
        # The viewer's page: the reader's copy carries the licence page at index 1.
        self.assertEqual(citation["locator"], {"page": 4, "source_page": 3, "section": "2 Bernoulli"})
        self.assertEqual(citation["selector"]["type"], "TextQuoteSelector")
        self.assertEqual(citation["readium_locator"]["locations"]["fragments"], ["page=4"])

        via_link = self.ok("resolve_citation", {"uri": citation["url"]})
        self.assertEqual(via_link["uri"], uri)

        lent = self.ok("resolve_citation", {"uri": self.uri(self.lcp, self.lcp_paragraphs[2])})
        self.assertNotIn("selector", lent)
        self.assertNotIn("section", lent["locator"])
        self.assertEqual(lent["access"]["tier"], "metadata")

    def test_cite_formats_references(self):
        uri = self.uri(self.entry, self.paragraphs[2])
        for style, marker in (("iso690", "Fluid Mechanics."), ("apa", "(n.d.)"), ("bibtex", "@book{")):
            with self.subTest(style=style):
                result = self.ok("cite", {"uris": [uri], "style": style})
                self.assertIn(marker, result["references"][0]["text"])
        csl = self.ok("cite", {"uris": [uri], "style": "csl-json"})["references"][0]["csl"]
        self.assertEqual((csl["title"], csl["page"]), ("Fluid Mechanics", "4"))

    def test_a_region_shows_caption_and_crop(self):
        region = self.ok("get_region", {"uri": uris.locator_uri(self.entry.pk, self.venturi.locator_key)})
        self.assertEqual((region["kind"], region["page"], region["label"]), ("figure", 4, "Obr. 2.1"))
        self.assertIn("crop_url", region)
        refused = self.call("get_region", {"uri": self.uri(self.entry, self.paragraphs[0])})
        self.assertTrue(refused["isError"])

    def test_a_region_a_decider_hid_is_left_out_of_every_tool(self):
        """#189: the address still resolves (to its page), but no caption, crop or link."""
        from apps.extraction.models import Region

        Region.objects.filter(pk=self.venturi.pk).update(band=Region.Band.HIDE)
        address = uris.locator_uri(self.entry.pk, self.venturi.locator_key)

        region = self.ok("get_region", {"uri": address})
        self.assertEqual((region["kind"], region["page"]), ("figure", 4))
        for leaked in ("label", "caption", "crop_url", "description"):
            self.assertNotIn(leaked, region)

        outline = self.ok("get_outline", {"entry_id": str(self.entry.pk)})
        self.assertNotIn(4, [item["page"] for item in outline["regions"]])

        result = self.call("read", {"uri": self.uri(self.entry, self.paragraphs[3]), "context": 1})
        self.assertNotIn(address, [block.get("uri") for block in result["content"]])
        self.assertEqual(result["structuredContent"]["regions"], [])

    def test_a_pack_labels_passages_and_resolves_them_later_for_its_owner_only(self):
        pack = self.ok("content_pack", {"query": "Bernoulli pressure speed", "budget_chars": 2000}, self.reader)
        self.assertTrue(pack["passages"])
        self.assertEqual(pack["passages"][0]["label"], "E1")
        self.assertTrue(ContentPack.objects.filter(key=pack["pack_id"], user=self.reader).exists())
        lent = [item for item in pack["withheld"] if str(self.lcp.pk) in item["uri"]]
        self.assertTrue(all(item["tier"] == "metadata" for item in lent))

        citation = self.ok("resolve_citation", {"pack_id": pack["pack_id"], "label": "E1"}, self.reader)
        self.assertEqual(citation["uri"], pack["passages"][0]["uri"])
        stranger = self.call("resolve_citation", {"pack_id": pack["pack_id"], "label": "E1"}, self.other_reader)
        self.assertTrue(stranger["isError"])

    def test_search_content_returns_addresses_with_snippets_where_readable(self):
        result = self.ok("search_content", {"q": "Bernoulli pressure", "kinds": ["passage"]})
        by_entry = {}
        for hit in result["items"]:
            by_entry.setdefault(hit["entry_id"], []).append(hit)
        self.assertIn(str(self.entry.pk), by_entry)
        readable = by_entry[str(self.entry.pk)][0]
        self.assertTrue(readable["uri"].startswith(f"evilflowers://entry/{self.entry.pk}/locator/"))
        self.assertEqual(readable["source"], readable["url"])
        self.assertTrue(readable["content"])
        for hit in by_entry.get(str(self.lcp.pk), []):
            self.assertIsNone(hit["content"])
        self.assertNotIn(str(self.secret.pk), by_entry)

    def test_cited_by_lists_visible_citing_books_only(self):
        from apps.extraction.models import Bibitem

        for run in (self.lcp_run, self.secret_run):
            Bibitem.objects.create(
                run=run, seq=0, page=5, raw="Fluid Mechanics.", entry=self.entry, locator_key=f"bib{run.pk.hex[:12]}"
            )
        result = self.ok("cited_by", {"entry_id": str(self.entry.pk)})
        self.assertEqual([item["entry_id"] for item in result["cited_by"]], [str(self.lcp.pk)])
        self.assertTrue(result["cited_by"][0]["url"].endswith(f"/cite/{self.lcp.pk}/bib{self.lcp_run.pk.hex[:12]}"))
        self.assertEqual(
            self.ok("cited_by", {"entry_id": str(self.lcp.pk)})["cites"][0]["entry_id"], str(self.entry.pk)
        )
        self.assertTrue(self.call("cited_by", {"entry_id": str(self.secret.pk)})["isError"])

    def test_my_access(self):
        mine = self.ok("my_access", {"entry_id": str(self.lcp.pk)}, self.reader)
        self.assertEqual((mine["tier"], mine["basis"], mine["policy"]), ("metadata", "none", "parity@1"))

    def test_the_prompt_and_the_resource_templates(self):
        from apps.mcp import prompts, resources

        rendered = prompts.get_prompt("answer_with_citations", {"question": "What is the Venturi effect?"})
        self.assertIn("content_pack", rendered["messages"][0]["content"]["text"])
        templates = {item["uriTemplate"] for item in resources.list_resource_templates(None)["resourceTemplates"]}
        self.assertTrue({"evilflowers://entry/{id}/outline", "evilflowers://entry/{id}/locator/{key}"} <= templates)
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        read = resources.read_resource(request, uris.outline_uri(self.lcp.pk))
        self.assertFalse(json.loads(read["contents"][0]["text"])["readable"])
