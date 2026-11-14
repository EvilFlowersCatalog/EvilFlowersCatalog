"""IP-030 Q1/Q6 in a chat bound to the reader's copy, whose viewer pages are offset from the original file.

A personalised copy starts with a licence page, so the viewer's page N is the original file's page N-1. Every
page a bound chat shows the model or the reader — citations, followed passages, study cards, the pack — must be
the viewer's.
"""

import json
from unittest import mock

from django.test import TestCase

from apps.assistant import document_tools, evidence, tools, turn
from apps.assistant.library import ToolContext
from apps.assistant.pdf import ReaderDocument
from apps.assistant.tests import fixtures
from apps.assistant.tests.test_ip030_tools import JsonClient
from apps.assistant.tests.test_turn_tools import ScriptedClient, _call
from apps.assistant.models import Chat
from apps.assistant import evaluation


def _call_of(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


class BoundChatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.rows, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)

    def setUp(self):
        from apps.extraction import references

        references.build(self.extraction)
        # Reader page 1 is the licence page; reader page N + 1 is original page N.
        pages = ["licence"] + [f"page {n} text about pressure" for n in range(1, 7)]
        self.document = ReaderDocument(pages, [], [None, 1, 2, 3, 4, 5, 6], acquisition=self.extraction.acquisition)
        self.ledger = evidence.Ledger()

    def _search(self, query="pressure"):
        candidates = [
            mock.Mock(paragraph=self.rows[2], score=0.9, page=4, sentences=[]),
        ]
        with (
            mock.patch("apps.extraction.reader.ReaderExtraction.has_paragraphs", return_value=True),
            mock.patch("apps.extraction.reader.ReaderExtraction.embed", return_value=[0.0]),
            mock.patch("apps.extraction.reader.ReaderExtraction.search_paragraphs", return_value=candidates),
        ):
            return document_tools.run(self.document, _call_of("search_paragraphs", query=query), [], None, self.ledger)

    def test_search_paragraphs_labels_with_the_viewers_page(self):
        payload, text = self._search()
        seen = json.loads(text)["paragraphs"][0]
        self.assertEqual((seen["label"], seen["page"]), ("E1", 4))
        self.assertNotIn("paragraph_id", seen)
        source = self.ledger.sources["E1"]
        self.assertEqual((source["page"], source["source_page"]), (4, 3))

        found, unknown = self.ledger.cite("Tlak klesá, keď rýchlosť rastie [E1].", fixtures.request_for(self.reader))
        self.assertEqual(unknown, [])
        self.assertEqual(found[0]["page"], 4)
        self.assertEqual(found[0]["sentence_page"], 4)  # the sentence is on original page 3
        self.assertIn("quads", found[0])

    def test_read_pdf_page_and_regions_are_labelled(self):
        _payload, text = document_tools.run(self.document, _call_of("read_pdf_page", page=2), [], None, self.ledger)
        self.assertEqual(json.loads(text)["pages"][0]["label"], "E1")
        _payload, text = document_tools.run(self.document, _call_of("list_regions"), [], None, self.ledger)
        regions = json.loads(text)["regions"]
        venturi = next(region for region in regions if "Venturiho" in (region.get("caption") or ""))
        self.assertEqual(venturi["page"], 5)  # original page 4
        self.assertNotIn("region_id", venturi)
        _payload, text = document_tools.run(
            self.document, _call_of("get_region", label=venturi["label"]), [], None, self.ledger
        )
        self.assertIn("Venturiho", json.loads(text)["printed_caption"])
        self.assertEqual(json.loads(text)["page"], 5)

    def test_follow_and_study_speak_the_viewers_pages(self):
        context = ToolContext(
            request=fixtures.request_for(self.reader),
            document=self.document,
            ledger=self.ledger,
            reading=tools.Reading(),
            language="sk",
        )
        self._search()
        payload, _text = tools.run(
            context.request, tools.FOLLOW, {"label": "E1", "relation": "next"}, ledger=self.ledger, context=context
        )
        self.assertEqual([p["page"] for p in payload["passages"]], [5, 6])
        # The section's paragraphs keep the labels they already have (E1 searched, E2-E3 followed).
        card = JsonClient({"points": [{"text": "a", "labels": ["E1"]}, {"text": "b", "labels": ["E3"]}]})
        with mock.patch("apps.assistant.llm.client", return_value=card):
            payload, _text = tools.run(
                context.request, tools.STUDY, {"mode": "walkthrough", "section": "2 Bernoulli"}, context=context
            )
        self.assertEqual(payload["study"]["data"]["page_range"], [4, 6])
        self.assertEqual(payload["entry_id"], str(self.entry.pk))

    def test_a_bound_turn_offers_the_swapped_tools_and_never_asks_for_old_markers(self):
        chat = Chat.objects.create(user=self.reader, entry=self.entry)
        client = ScriptedClient([[_call("read_pdf_page", page=2)], "Strana hovorí o tlaku [E1]."])
        with mock.patch.object(turn, "make_client", return_value=client):
            frames = list(
                turn.run(fixtures.request_for(self.reader), chat, "O čom je strana 2?", document=self.document)
            )
        names = [spec["function"]["name"] for spec in client.requests[0]["tools"]]
        self.assertIn("follow", names)
        self.assertNotIn("search_pdf", names)
        self.assertNotIn("list_page_regions", names)
        self.assertEqual(len(names), len(set(names)))
        prompt = json.dumps(client.requests[0]["messages"], ensure_ascii=False)
        self.assertNotIn("[[page:", prompt)
        self.assertNotIn("[[region:", prompt)
        citations = dict(evaluation.parse_frames(frames))["citations"]["citations"]
        self.assertEqual((citations[0]["label"], citations[0]["page"]), ("E1", 2))
