"""IP-030 §2: one ledger with E#/F#, citations completed for the viewer, resolved while streaming."""

import random

from django.test import SimpleTestCase, TestCase

from apps.assistant import citations, evidence
from apps.assistant.tests import fixtures


class LedgerSeriesTests(SimpleTestCase):
    def test_passages_and_figures_number_separately_and_keep_their_labels(self):
        ledger = evidence.Ledger()
        self.assertEqual(ledger.issue({"paragraph_id": "p1", "page": 1})[0], "E1")
        self.assertEqual(ledger.issue({"region_id": "r1", "kind": "figure", "page": 4})[0], "F1")
        self.assertEqual(ledger.issue({"paragraph_id": "p2", "page": 2})[0], "E2")
        self.assertEqual(ledger.issue({"region_id": "r1", "kind": "figure"}), ("F1", False))
        rebuilt = evidence.Ledger(ledger.sources)
        self.assertEqual(rebuilt.issue({"region_id": "r2"})[0], "F2")

    def test_cite_reads_both_series_and_the_citing_sentence(self):
        ledger = evidence.Ledger({"E1": {"page": 1}, "F1": {"region_id": "r", "page": 4}})
        found, unknown = ledger.cite("Pressure falls [E1, F1]. See it [f1]. Nope [F9].")
        self.assertEqual([c["label"] for c in found], ["E1", "F1"])
        self.assertEqual(unknown, ["F9"])
        self.assertEqual(
            evidence.labels_with_sentences("First thing. Pressure falls in the throat [E1].")[0],
            ("E1", "Pressure falls in the throat"),
        )

    def test_page_pseudo_ids_stay_out_of_the_citation(self):
        ledger = evidence.Ledger({"E1": {"kind": "page", "paragraph_id": "page:x:3", "page": 3}})
        self.assertNotIn("paragraph_id", ledger.cite("See [E1].")[0][0])


class ResolverTests(SimpleTestCase):
    LEDGER = evidence.Ledger({"E1": {"page": 1}, "E2": {"page": 2}, "F1": {"region_id": "r", "page": 4}})
    TEXT = "Pressure falls [E1]. The tube [F1] shows it [E1, E2]. Arrays [0] and [Example] stay. End [E"

    def _run(self, chunks):
        resolver = citations.Resolver(self.LEDGER)
        out, found = "", []
        for chunk in chunks:
            text, new = resolver.feed(chunk)
            out += text
            found += new
            # A partial label is never let through.
            self.assertFalse(out.endswith("[E") or out.endswith("[F"))
        text, new = resolver.finish()
        return out + text, found + new

    def test_any_split_of_the_stream_passes_the_text_through_unchanged(self):
        rng = random.Random(7)
        for _ in range(50):
            cuts = sorted(rng.sample(range(1, len(self.TEXT)), 8))
            chunks = [self.TEXT[a:b] for a, b in zip([0] + cuts, cuts + [len(self.TEXT)])]
            text, found = self._run(chunks)
            self.assertEqual(text, self.TEXT)
            self.assertEqual([c["label"] for c in found], ["E1", "F1", "E2"])

    def test_a_label_reported_in_an_earlier_round_is_not_reported_again(self):
        seen = {"E1"}
        resolver = citations.Resolver(self.LEDGER, seen=seen)
        _text, found = resolver.feed("Again [E1] and [E2].")
        self.assertEqual([c["label"] for c in found], ["E2"])


class CompleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)
        cls.licensed, _run, cls.lcp_paragraphs, cls.lcp_figure, _ = fixtures.publication(
            cls.reader, cls.open, title="Licensed", readium=True
        )

    def _source(self, entry, paragraph):
        return {
            "kind": "passage",
            "entry_id": str(entry.pk),
            "page": paragraph.page,
            "paragraph_id": str(paragraph.pk),
        }

    def test_the_best_sentence_is_the_one_sharing_most_words(self):
        paragraph = self.paragraphs[2]
        citation = citations.complete(
            "E1",
            self._source(self.entry, paragraph),
            "Where the speed rises the pressure falls",
            fixtures.request_for(self.reader),
        )
        self.assertTrue(citation["readable"])
        self.assertEqual(citation["bbox"], fixtures.BBOX)
        self.assertIn("Pressure falls where the speed rises", citation["quote"])
        self.assertAlmostEqual(citation["quads"][0]["y0"], 0.21)  # the second sentence's quads
        self.assertEqual(citation["paragraph_id"], str(paragraph.pk))

    def test_figures_carry_caption_and_crop_where_readable(self):
        source = {"kind": "figure", "entry_id": str(self.entry.pk), "page": 4, "region_id": str(self.venturi.pk)}
        citation = citations.complete("F1", source, "", fixtures.request_for(self.reader))
        self.assertIn("Venturiho", citation["caption"])
        self.assertIn(f"/regions/{self.venturi.pk}", citation["image_url"])

    def test_readium_lcp_text_never_leaves_through_a_citation(self):
        request = fixtures.request_for(self.reader)
        passage = citations.complete("E1", self._source(self.licensed, self.lcp_paragraphs[2]), "speed", request)
        figure = citations.complete(
            "F1",
            {"kind": "figure", "entry_id": str(self.licensed.pk), "region_id": str(self.lcp_figure.pk)},
            "",
            request,
        )
        self.assertFalse(passage["readable"])
        self.assertNotIn("quote", passage)
        self.assertIn("bbox", passage)  # the page and box still lead the borrower there
        self.assertNotIn("caption", figure)
        self.assertNotIn("image_url", figure)

    def test_a_paragraph_replaced_by_a_newer_run_still_resolves_to_its_page(self):
        source = {"kind": "passage", "page": 7, "paragraph_id": "00000000-0000-4000-8000-000000000000"}
        citation = citations.complete("E1", source, "", fixtures.request_for(self.reader))
        self.assertEqual(citation["page"], 7)
        self.assertNotIn("bbox", citation)
