"""Semantic highlight finding and region tools (IP-018 Phases 5 and 6b), without a database."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.assistant import semantic
from apps.assistant.explain import GET_REGION, LIST_PAGE_REGIONS, run_document_tool
from apps.assistant.pdf import ReaderDocument

BOX = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}


def _candidate(seq, score, text, page=3):
    paragraph = SimpleNamespace(seq=seq, section="Entropy", text=text)
    first = text.index(".") + 1
    sentences = [
        SimpleNamespace(seq=0, start=0, end=first, page=page, quads=[BOX]),
        SimpleNamespace(seq=1, start=first + 1, end=len(text), page=page, quads=[BOX]),
    ]
    return SimpleNamespace(paragraph=paragraph, score=score, page=page, sentences=sentences)


class SemanticFindTests(SimpleTestCase):
    def setUp(self):
        self.candidates = [
            _candidate(4, 0.41, "Entropy is the expected information. It is measured in bits."),
            _candidate(9, 0.72, "A fair coin has one bit of entropy. Two coins have two bits."),
        ]
        self.extraction = mock.Mock()
        self.extraction.has_paragraphs.return_value = True
        self.extraction.embed.return_value = [0.0] * 1024
        self.extraction.search_paragraphs.return_value = self.candidates
        self.extraction.page_size.return_value = (595.0, 842.0)

    def _find(self, answer):
        with (
            mock.patch("apps.extraction.reader.ReaderExtraction.for_document", return_value=self.extraction),
            mock.patch.object(semantic, "OllamaClient"),
            mock.patch.object(semantic, "_complete", return_value=(answer, [], 10)) as complete,
        ):
            return semantic.find(object(), prompt="what is entropy", language="en"), complete

    def test_model_picks_numbered_sentences_and_text_is_the_stored_one(self):
        result, complete = self._find('{"summary": "Definition.", "keep": [{"id": 3, "note": "example"}, {"id": 1}]}')
        prompt = complete.call_args.args[1][1]["content"]
        self.assertIn("1. Entropy is the expected information.", prompt)
        self.assertIn("score 0.72", prompt)
        self.assertEqual(
            [pick.text for pick in result.picks],
            ["Entropy is the expected information.", "A fair coin has one bit of entropy."],
        )
        self.assertEqual(result.picks[1].note, "example")
        self.assertEqual(result.summary, "Definition.")
        self.assertEqual((result.candidates, result.sentences), (2, 4))

    def test_numbers_outside_the_candidates_are_ignored(self):
        result, _complete = self._find('{"summary": "", "keep": [{"id": 99}, {"id": "x"}]}')
        self.assertEqual(result.picks, [])

    def test_without_paragraph_index_falls_back(self):
        self.extraction.has_paragraphs.return_value = False
        result, complete = self._find("{}")
        self.assertIsNone(result)
        complete.assert_not_called()


class RegionToolTests(SimpleTestCase):
    def test_unprocessed_publication_says_so(self):
        document = ReaderDocument(["page one"], toc=[])
        call = {"function": {"name": LIST_PAGE_REGIONS, "arguments": {"page": 1}}}
        self.assertIn("error", json.loads(run_document_tool(document, call, [])))

    def test_regions_cost_no_page_budget(self):
        extraction = mock.Mock()
        extraction.region.return_value = {"id": "r", "kind": "table", "page": 1, "markdown": "| a |"}
        document = ReaderDocument(["page one"], toc=[])
        document._extraction = extraction
        pages_read = []
        answer = json.loads(
            run_document_tool(
                document,
                {"function": {"name": GET_REGION, "arguments": {"region_id": "8c0e2b5e-0000-4000-8000-000000000000"}}},
                pages_read,
            )
        )
        self.assertEqual(answer["region"]["markdown"], "| a |")
        self.assertEqual(pages_read, [])
