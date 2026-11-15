"""#171: locators survive a re-extraction; citations, sections and the API resolve through them."""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.assistant import citations, evidence
from apps.assistant.tests import fixtures
from apps.extraction import locators, services
from apps.extraction.models import ExtractionRun, Locator, Paragraph, Region, Sentence
from apps.extraction.outline import outline
from apps.graph import projection
from apps.mcp import uris

EDITED = "Bernoulli's principle relates the pressure and the speed of a fluid. Pressure falls where the speed rises."

#: The FLUIDS fixture extracted again: the introduction's first paragraph is gone, Bernoulli's is edited, the last
#: one moved a page down; the rest is unchanged.
REEXTRACTED = [
    (2, "1 Introduction", fixtures.FLUIDS[1][2]),
    (3, "2 Bernoulli", EDITED),
    (4, "2 Bernoulli", fixtures.FLUIDS[3][2]),
    (6, "2 Bernoulli", fixtures.FLUIDS[4][2]),
]
MOVED_BOX = {"x0": 0.3, "y0": 0.5, "x1": 0.95, "y1": 0.8}


def reextract(run, paragraphs=REEXTRACTED, venturi_bbox=MOVED_BOX) -> ExtractionRun:
    """A newer run of the same acquisition, made active the way the last job report does it."""
    new = ExtractionRun.objects.create(
        acquisition=run.acquisition,
        pipeline_version=run.pipeline_version + 1,
        status=ExtractionRun.Status.RUNNING,
        page_count=6,
        jobs={"text": ExtractionRun.JobStatus.DONE},
    )
    for seq, (page, section, text) in enumerate(paragraphs):
        paragraph = Paragraph.objects.create(
            run=new, page=page, seq=seq, section=section, bbox=fixtures.BBOX, text=text
        )
        Sentence.objects.create(paragraph=paragraph, seq=0, start=0, end=len(text), page=page, quads=[fixtures.BBOX])
    Region.objects.create(
        run=new,
        kind=Region.Kind.FIGURE,
        page=4,
        seq=0,
        bbox=venturi_bbox,
        caption="Obr. 2.1: Venturiho trubica a pokles tlaku v zúžení",
        printed_caption="Obr. 2.1: Venturiho trubica a pokles tlaku v zúžení",
    )
    Region.objects.create(
        run=new,
        kind=Region.Kind.FIGURE,
        page=2,
        seq=0,
        bbox=fixtures.BBOX,
        printed_caption="Figure 1.1: Shear on a fluid layer",
    )
    with (
        patch("apps.extraction.services.delete_files"),
        patch("apps.extraction.tasks.queue_post_process"),
        patch("apps.extraction.services.update_embedding"),
    ):
        services._finish(new)
    new.save()
    return new


class KeyTests(SimpleTestCase):
    def test_keys_ignore_whitespace_and_depend_on_page_and_entry(self):
        key = locators.paragraph_key("e1", 3, "Pressure  falls\nwhere")
        self.assertEqual(key, locators.paragraph_key("e1", 3, " Pressure falls where "))
        self.assertEqual(len(key), 16)
        self.assertNotEqual(key, locators.paragraph_key("e1", 4, "Pressure falls where"))
        self.assertNotEqual(key, locators.paragraph_key("e2", 3, "Pressure falls where"))
        self.assertNotEqual(key, locators.paragraph_key("e1", 3, "Pressure falls where", ordinal=1))

    def test_a_region_key_survives_a_box_jitter_below_the_grid(self):
        box = {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.3}
        jittered = {"x0": 0.1004, "y0": 0.2003, "x1": 0.9, "y1": 0.3}
        self.assertEqual(
            locators.region_key("e", "figure", 4, "Obr. 2.1", box),
            locators.region_key("e", "figure", 4, "Obr. 2.1", jittered),
        )

    def test_section_keys_are_run_independent_and_ordinal_on_collision(self):
        keys = locators.section_keys("e", ["Úvod", "1 Pressure", "Úvod ", "Uvod"])
        self.assertEqual(keys["Úvod"], projection.section_key("e", "Úvod"))
        self.assertNotEqual(keys["Úvod"], keys["Úvod "])  # equal after normalising: the second gets ordinal 1
        self.assertEqual(keys["Úvod "], projection.section_key("e", "Úvod", 1))
        self.assertNotEqual(keys["Úvod"], keys["Uvod"])  # accents are text, not noise
        self.assertRegex(keys["1 Pressure"], r"^[0-9a-f]{20}$")  # what the MCP graph tools accept

    def test_uris(self):
        uri = uris.locator_uri("6a3c5b7e-0000-4000-8000-000000000001", "abcdefgh12345678")
        self.assertEqual(uri, "evilflowers://entry/6a3c5b7e-0000-4000-8000-000000000001/locator/abcdefgh12345678")
        reference = uris.parse(uri)
        self.assertEqual((reference.part, reference.key), (uris.LOCATOR, "abcdefgh12345678"))
        self.assertEqual(uris.parse(uris.outline_uri("6a3c5b7e-0000-4000-8000-000000000001")).part, uris.OUTLINE)
        self.assertIsNone(uris.parse("evilflowers://entry/6a3c5b7e-0000-4000-8000-000000000001/locator/../x"))
        self.assertIsNone(uris.parse("evilflowers://feed/6a3c5b7e-0000-4000-8000-000000000001/outline"))
        self.assertEqual(
            uris.parse("evilflowers://entry/6a3c5b7e-0000-4000-8000-000000000001"),
            uris.ResourceRef("entry", "6a3c5b7e-0000-4000-8000-000000000001"),
        )


class LocatorLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, cls.shear = fixtures.publication(cls.reader, cls.open)
        locators.assign(cls.extraction)

    def key(self, row):
        row.refresh_from_db()
        return row.locator_key

    def test_assign_keys_every_unit_and_is_idempotent(self):
        self.assertTrue(all(self.key(paragraph) for paragraph in self.paragraphs))
        kinds = sorted(Locator.objects.filter(entry=self.entry).values_list("kind", flat=True))
        self.assertEqual(kinds, ["figure", "figure"] + ["paragraph"] * 5 + ["section", "section"])
        self.assertEqual(
            locators.assign(self.extraction), {"locators": 9, "created": 0, "refreshed": 0, "detached": 0}
        )
        locator = Locator.objects.get(key=self.key(self.paragraphs[2]))
        self.assertEqual((locator.first_run, locator.last_run), (self.extraction.pk, self.extraction.pk))
        self.assertEqual(locator.prefix, fixtures.FLUIDS[1][2][-locators.CONTEXT_CHARS :])
        self.assertTrue(locator.suffix.startswith("The Venturi tube"))

    def test_a_repeated_paragraph_on_one_page_gets_an_ordinal(self):
        entry, run, *_ = fixtures.publication(
            self.reader,
            self.open,
            title="Headers",
            paragraphs=[(1, "A", "Running header"), (1, "A", "Running header")],
        )
        locators.assign(run)
        keys = list(run.paragraphs.order_by("seq").values_list("locator_key", flat=True))
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(keys[0], locators.paragraph_key(entry.pk, 1, "Running header"))

    def test_reextraction_keeps_keys_and_resolves_each_case(self):
        unchanged, edited, removed, moved = (self.key(self.paragraphs[i]) for i in (1, 2, 0, 4))
        venturi, shear = self.key(self.venturi), self.key(self.shear)
        new = reextract(self.extraction)
        self.assertFalse(ExtractionRun.objects.filter(pk=self.extraction.pk).exists())

        # Unchanged: the same key in the new run, resolved exactly.
        resolution = locators.resolve(self.entry.pk, unchanged)
        self.assertEqual(resolution.resolution, locators.EXACT)
        self.assertEqual(resolution.paragraph.run_id, new.pk)
        self.assertEqual(resolution.paragraph.locator_key, unchanged)
        self.assertEqual(locators.resolve(self.entry.pk, shear).resolution, locators.EXACT)

        # Edited and moved: found again by the quote; their old keys are detached.
        resolution = locators.resolve(self.entry.pk, edited)
        self.assertEqual((resolution.resolution, resolution.paragraph.text), (locators.RELOCATED, EDITED))
        resolution = locators.resolve(self.entry.pk, moved)
        self.assertEqual((resolution.resolution, resolution.source_page), (locators.RELOCATED, 6))
        resolution = locators.resolve(self.entry.pk, venturi)
        self.assertEqual((resolution.resolution, resolution.region.bbox), (locators.RELOCATED, MOVED_BOX))

        # Removed: only the page is left, and the locator is kept, detached.
        resolution = locators.resolve(self.entry.pk, removed)
        self.assertEqual((resolution.resolution, resolution.source_page), (locators.PAGE_ONLY, 1))
        self.assertIsNone(resolution.bbox)
        detached = set(
            Locator.objects.filter(entry=self.entry, detached_at__isnull=False).values_list("key", flat=True)
        )
        self.assertEqual(detached, {edited, removed, moved, venturi})
        self.assertIsNone(Locator.objects.get(key=unchanged).detached_at)
        self.assertEqual(Locator.objects.get(key=unchanged).last_run, new.pk)
        self.assertEqual(Locator.objects.get(key=unchanged).first_run, self.extraction.pk)
        self.assertIsNone(locators.resolve(self.entry.pk, "nosuchkey0000000"))

    def test_a_key_that_comes_back_is_attached_again(self):
        removed = self.key(self.paragraphs[0])
        newer = reextract(self.extraction)
        self.assertIsNotNone(Locator.objects.get(key=removed).detached_at)
        reextract(newer, paragraphs=fixtures.FLUIDS, venturi_bbox=fixtures.BBOX)
        locator = Locator.objects.get(key=removed)
        self.assertIsNone(locator.detached_at)
        self.assertEqual(locators.resolve(self.entry.pk, removed).resolution, locators.EXACT)

    def test_section_keys_are_equal_before_and_after_the_reextraction(self):
        before = projection.section_keys(self.entry.pk, [section.title for section in outline(self.extraction)])
        new = reextract(self.extraction)
        resolution = locators.resolve(self.entry.pk, before["2 Bernoulli"])
        self.assertEqual(resolution.resolution, locators.EXACT)
        self.assertEqual(resolution.section.title, "2 Bernoulli")
        self.assertEqual(resolution.run.pk, new.pk)
        self.assertEqual(locators.resolve(self.entry.pk, before["1 Introduction"]).source_page, 2)


class CitationThroughLocatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)
        cls.licensed, cls.lcp_run, cls.lcp_paragraphs, _f, _s = fixtures.publication(
            cls.reader, cls.open, title="Licensed", readium=True
        )
        locators.assign(cls.extraction)
        locators.assign(cls.lcp_run)

    def source(self, entry, paragraph, ledger=None):
        ledger = ledger or evidence.Ledger()
        label, _new = ledger.issue(
            {"kind": "passage", "entry_id": str(entry.pk), "page": paragraph.page, "paragraph_id": str(paragraph.pk)}
        )
        return ledger.sources[label]

    def test_the_ledger_keeps_the_locator_key(self):
        payload = {
            "items": [
                {
                    "entry_id": str(self.entry.pk),
                    "passages": [{"text": "x", "page": 3, "paragraph_id": str(self.paragraphs[2].pk)}],
                }
            ]
        }
        ledger = evidence.Ledger()
        evidence.label_passages(payload, ledger)
        self.paragraphs[2].refresh_from_db()
        self.assertEqual(ledger.sources["E1"]["locator_key"], self.paragraphs[2].locator_key)
        self.assertNotIn("locator_key", evidence.strip(payload)["items"][0]["passages"][0])
        figures = evidence.label_figures({"figures": [{"region_id": str(self.venturi.pk), "page": 4}]}, ledger)
        self.venturi.refresh_from_db()
        self.assertEqual(figures["evidence"]["F1"]["locator_key"], self.venturi.locator_key)

    def test_a_citation_from_before_the_reextraction_lands_on_the_same_paragraph(self):
        unchanged = self.source(self.entry, self.paragraphs[3])
        edited = self.source(self.entry, self.paragraphs[2])
        reextract(self.extraction)
        request = fixtures.request_for(self.reader)

        citation = citations.complete("E1", unchanged, "The Venturi tube shows the effect", request)
        self.assertEqual(citation["resolution"], "exact")
        new_paragraph = Paragraph.objects.get(run__acquisition__entry=self.entry, page=4)
        self.assertEqual(citation["paragraph_id"], str(new_paragraph.pk))
        self.assertIn("Venturi", citation["quote"])
        self.assertIn("bbox", citation)

        citation = citations.complete("E2", edited, "Pressure falls where the speed rises", request)
        self.assertEqual(citation["resolution"], "relocated")
        self.assertIn("speed", citation["quote"])
        self.assertEqual(citation["page"], 3)

    def test_a_moved_paragraph_reports_its_new_page(self):
        moved = self.source(self.entry, self.paragraphs[4])
        reextract(self.extraction)
        citation = citations.complete("E1", moved, "", fixtures.request_for(self.reader))
        self.assertEqual((citation["resolution"], citation["page"]), ("relocated", 6))

    def test_lcp_redaction_is_unchanged(self):
        source = self.source(self.licensed, self.lcp_paragraphs[2])
        self.assertTrue(source["locator_key"])
        citation = citations.complete("E1", source, "speed", fixtures.request_for(self.reader))
        self.assertEqual(citation["resolution"], "exact")
        self.assertFalse(citation["readable"])
        self.assertNotIn("quote", citation)
        self.assertIn("bbox", citation)


class LocatorApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.hidden = fixtures.catalog(cls.reader, public=False, name="hidden")
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)
        cls.licensed, cls.lcp_run, cls.lcp_paragraphs, _f, _s = fixtures.publication(
            cls.reader, cls.open, title="Licensed", readium=True
        )
        cls.secret, cls.secret_run, cls.secret_paragraphs, _f2, _s2 = fixtures.publication(
            cls.reader, cls.hidden, title="Secret"
        )
        for run in (cls.extraction, cls.lcp_run, cls.secret_run):
            locators.assign(run)

    def get(self, entry, key, status=200, **params):
        response = self.client.get(f"/api/v1/locators/{entry.pk}/{key}", params)
        self.assertEqual(response.status_code, status, response.content[:400])
        return response.json().get("response")

    def key(self, row):
        row.refresh_from_db()
        return row.locator_key

    def test_an_open_paragraph(self):
        data = self.get(self.entry, self.key(self.paragraphs[2]), sentence="Where the speed rises the pressure falls")
        self.assertEqual((data["kind"], data["resolution"], data["source_page"]), ("paragraph", "exact", 3))
        self.assertEqual(data["page"], 4)  # the license page counts in the reader's numbering
        self.assertTrue(data["readable"])
        self.assertIn("Bernoulli", data["quote"])
        self.assertAlmostEqual(data["quads"][0]["y0"], 0.21)  # the best sentence: the second one
        readium = data["readium"]
        self.assertEqual(readium["locations"], {"position": 4, "fragments": ["page=4"]})
        self.assertEqual(readium["text"]["highlight"], data["quote"])
        self.assertIn(f"/acquisitions/{self.extraction.acquisition_id}", readium["href"])

    def test_readium_lcp_never_returns_the_text(self):
        data = self.get(self.licensed, self.key(self.lcp_paragraphs[2]))
        self.assertFalse(data["readable"])
        self.assertIsNone(data["quote"])
        self.assertEqual(data["readium"]["text"], {})
        self.assertEqual(data["resolution"], "exact")  # the place is still given

    def test_hidden_entries_and_unknown_keys_are_not_found(self):
        self.get(self.secret, self.key(self.secret_paragraphs[0]), status=404)
        self.get(self.entry, "nosuchkey0000000", status=404)
        # A key of another entry does not resolve under this one.
        self.get(self.entry, self.key(self.lcp_paragraphs[0]), status=404)

    def test_mcp_outline_and_locator_resources(self):
        from apps.mcp import resources

        request = fixtures.request_for(self.reader)
        listed = json.loads(resources.read_resource(request, uris.outline_uri(self.entry.pk))["contents"][0]["text"])
        self.assertEqual([s["title"] for s in listed["sections"]], ["1 Introduction", "2 Bernoulli"])
        located = json.loads(resources.read_resource(request, listed["sections"][1]["uri"])["contents"][0]["text"])
        self.assertEqual((located["kind"], located["resolution"]), ("section", "exact"))
        lcp = json.loads(resources.read_resource(request, uris.outline_uri(self.licensed.pk))["contents"][0]["text"])
        self.assertEqual({s["title"] for s in lcp["sections"]}, {None})
