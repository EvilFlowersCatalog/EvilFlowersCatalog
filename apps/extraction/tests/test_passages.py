"""Search inside publications: `GET /api/v1/entries/passages` (#60, IP-021)."""

from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import Acquisition, Catalog, Entry
from apps.core.search import embeddings
from apps.core.tests.test_search_golden import SearchFixtureMixin
from apps.core.tests.test_search_hybrid import axis
from apps.extraction.models import ExtractionRun, Paragraph

MODEL = "test-model@1"
URL = "/api/v1/entries/passages"
BOX = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}


def blend(primary: int, secondary: int, weight: float) -> list:
    """A unit vector between two axes: cosine `weight` to the first (weight² + rest² = 1)."""
    vector = axis(primary)
    vector[primary] = weight
    vector[secondary] = (1 - weight**2) ** 0.5
    return vector


@override_settings(
    EVILFLOWERS_EMBEDDING_URL="http://tei.test",
    EVILFLOWERS_EMBEDDING_MODEL=MODEL,
    EVILFLOWERS_SEARCH_PASSAGE_MIN_SCORE=0.5,
)
class PassageSearchTests(SearchFixtureMixin, TestCase):
    """The query lies on axis 0. `ml` has the best paragraph on it, `strojove` a weaker one."""

    @classmethod
    def setUpTestData(cls):
        cls.build_corpus()
        cls.paragraph(cls.keys["ml"], [(3, "Neural networks learn from data.", axis(0)), (4, "Off topic.", axis(5))])
        cls.paragraph(cls.keys["strojove"], [(7, "Učenie z dát.", blend(0, 1, 0.9)), (8, "Viac.", blend(0, 2, 0.8))])
        # Below the threshold, another model, and a private catalog: never returned.
        cls.paragraph(cls.keys["climate"], [(1, "Weather.", blend(0, 3, 0.3))])
        cls.paragraph(cls.keys["rudin"], [(1, "Analysis.", axis(0))], model="other-model@2")
        private = Catalog.objects.create(creator=cls.owner, title="Closed", url_name="closed", is_public=False)
        cls.hidden = Entry.objects.create(creator=cls.owner, catalog=private, title="Hidden")
        cls.paragraph(cls.hidden.pk, [(1, "Secret.", axis(0))])

    @classmethod
    def paragraph(cls, entry_id, rows, model=MODEL):
        acquisition = Acquisition.objects.create(
            entry_id=entry_id, mime=Acquisition.AcquisitionMIME.PDF, content=f"catalogs/x/{entry_id}.pdf"
        )
        run = ExtractionRun.objects.create(
            acquisition=acquisition,
            pipeline_version=2,
            status=ExtractionRun.Status.DONE,
            is_active=True,
            provenance={"paragraphs": {"embedding_model": model}},
        )
        for seq, (page, text, vector) in enumerate(rows):
            Paragraph.objects.create(run=run, page=page, seq=seq, bbox=BOX, text=text, embedding=vector)

    def search(self, vector=axis(0), status=200, **params):
        with mock.patch("apps.core.search.embeddings.try_embed_query", return_value=vector):
            response = self.client.get(URL, {"query": "machine learning", **params})
        self.assertEqual(response.status_code, status, response.content[:400])
        return response.json()["response"] if status == 200 else response.json()

    def test_best_paragraph_per_publication_best_first(self):
        result = self.search()
        self.assertFalse(result["degraded"])
        # ml, strojove, climate are searchable for an anonymous reader; rudin (other model) and hidden are not.
        self.assertEqual(result["indexed"], 3)
        self.assertEqual(
            [item["entry_id"] for item in result["items"]], [str(self.keys["ml"]), str(self.keys["strojove"])]
        )
        best = result["items"][0]["passages"]
        self.assertEqual(len(best), 1)
        self.assertEqual(
            (best[0]["page"], best[0]["text"], best[0]["score"]), (3, "Neural networks learn from data.", 1.0)
        )

    def test_per_entry_and_limit(self):
        result = self.search(per_entry=3, limit=1)
        self.assertEqual(len(result["items"]), 1)
        result = self.search(per_entry=3)
        strojove = result["items"][1]["passages"]
        self.assertEqual([passage["page"] for passage in strojove], [7, 8])

    def test_entry_filters_narrow_the_search(self):
        result = self.search(language_code="sk")
        self.assertEqual([item["entry_id"] for item in result["items"]], [str(self.keys["strojove"])])

    def test_licensed_entries_are_found_without_their_text(self):
        Entry.objects.filter(pk=self.keys["ml"]).update(config={"readium_enabled": True})
        passage = self.search()["items"][0]["passages"][0]
        self.assertEqual(passage["page"], 3)
        self.assertIsNone(passage["text"])

    def test_tei_down_says_so(self):
        result = self.search(vector=None)
        self.assertTrue(result["degraded"])
        self.assertEqual(result["items"], [])

    def test_tei_info_down_says_so(self):
        with mock.patch("apps.core.search.embeddings.model_id", side_effect=embeddings.EmbeddingError("down")):
            result = self.search()
        self.assertTrue(result["degraded"])

    def test_query_is_required(self):
        self.search(status=400, query="  ")

    # --- #113: words, snippets, catalog ---

    def test_a_phrase_only_in_the_text_is_found_with_a_marked_snippet(self):
        # Far from the query by meaning (axis 5), so only the word channel can find it.
        self.paragraph(self.keys["climate"], [(12, "Teória a prax vzdelávania dospelých <v praxi>.", axis(5))])
        result = self.search(vector=axis(9), query='"vzdelavania dospelych"')
        (item,) = result["items"]
        self.assertEqual(item["entry_id"], str(self.keys["climate"]))
        self.assertEqual(item["catalog_id"], str(Entry.objects.get(pk=self.keys["climate"]).catalog_id))
        (passage,) = item["passages"]
        self.assertEqual(passage["page"], 12)
        self.assertEqual(passage["matched"], ["words"])
        self.assertIn("<mark>vzdelávania</mark>", passage["snippet"])
        # The text is escaped: `<mark>` is the only markup.
        self.assertIn("&lt;v praxi", passage["snippet"])
        self.assertNotIn("<v", passage["snippet"])

    def test_words_still_work_when_embeddings_are_down(self):
        result = self.search(vector=None, query="Neural networks")
        self.assertTrue(result["degraded"])
        self.assertEqual([item["entry_id"] for item in result["items"]], [str(self.keys["ml"])])
        # rudin's paragraphs were embedded by another model: not searchable by meaning, but by words.
        self.assertEqual(result["searchable"], 4)

    def test_both_channels_agree(self):
        passage = self.search(query="neural networks")["items"][0]["passages"][0]
        self.assertEqual(passage["matched"], ["semantic", "words"])
        self.assertEqual(passage["score"], 1.0)

    def test_licensed_text_is_found_by_words_but_neither_text_nor_snippet_is_shown(self):
        Entry.objects.filter(pk=self.keys["ml"]).update(config={"readium_enabled": True})
        passage = self.search(vector=None, query="Neural networks")["items"][0]["passages"][0]
        self.assertIsNone(passage["text"])
        self.assertIsNone(passage["snippet"])

    def test_hidden_catalogs_stay_hidden_from_words_too(self):
        result = self.search(vector=None, query="Secret")
        self.assertEqual(result["items"], [])
