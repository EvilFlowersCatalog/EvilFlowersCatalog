"""Evidence labels on library passages and citations in answers (IP-024 §1, §3)."""

import json
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.assistant import evaluation, evidence, tools, turn
from apps.assistant.models import Chat, ChatMessage
from apps.assistant.tests.test_turn_tools import ScriptedClient, _call
from apps.core.models import AuthSource, Catalog, Entry, User


def _payload(entry_id, *passages):
    return {
        "items": [{"entry_id": entry_id, "title": "Fluid Mechanics", "passages": list(passages)}],
        "searched_publications": 3,
    }


def _passage(page, text="Bernoulli's principle relates pressure and speed.", paragraph_id=None):
    return {"paragraph_id": paragraph_id or str(uuid4()), "page": page, "section": "2.4", "score": 0.8, "text": text}


class LedgerTests(SimpleTestCase):
    def test_labels_continue_and_a_known_paragraph_keeps_its_label(self):
        ledger = evidence.Ledger()
        first = _passage(47)
        evidence.label_passages(_payload("a", first, _passage(48)), ledger)
        second = _payload("a", _passage(12), dict(first))
        evidence.label_passages(second, ledger)
        labels = [passage["label"] for passage in second["items"][0]["passages"]]
        self.assertEqual(labels, ["E3", "E1"])
        self.assertEqual(set(second[evidence.EVIDENCE_KEY]), {"E1", "E3"})

    def test_withheld_text_gets_no_label(self):
        payload = _payload("a", _passage(5, text=None))
        evidence.label_passages(payload, evidence.Ledger())
        self.assertNotIn("label", payload["items"][0]["passages"][0])
        self.assertNotIn(evidence.EVIDENCE_KEY, payload)

    def test_cite_resolves_groups_and_reports_unknown_labels(self):
        ledger = evidence.Ledger({"E1": {"entry_id": "a", "page": 4}, "E2": {"entry_id": "b", "page": 9}})
        citations, unknown = ledger.cite("Pressure falls [E2]. Speed rises [e1, E7]. Again [E2][E9].")
        self.assertEqual([c["label"] for c in citations], ["E2", "E1"])
        self.assertEqual(citations[0], {"label": "E2", "entry_id": "b", "page": 9})
        self.assertEqual(unknown, ["E7", "E9"])

    def test_text_without_labels(self):
        self.assertEqual(evidence.Ledger().cite("Nothing cited [1] [[page:3]]."), ([], []))

    def test_the_model_sees_labels_but_not_the_bookkeeping(self):
        payload = evidence.label_passages(_payload("a", _passage(47)), evidence.Ledger())
        seen = tools.slim(tools.SEARCH_PASSAGES, payload)
        self.assertNotIn(evidence.EVIDENCE_KEY, seen)
        self.assertEqual(seen["items"][0]["passages"][0]["label"], "E1")
        self.assertNotIn("paragraph_id", seen["items"][0]["passages"][0])
        # The full payload keeps both for the ledger.
        self.assertIn("paragraph_id", payload["items"][0]["passages"][0])


class CitationTurnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = User(username="reader", name="Reader", surname="Tester", auth_source=source)
        cls.reader.set_unusable_password()
        cls.reader.save()
        catalog = Catalog.objects.create(creator=cls.reader, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(creator=cls.reader, catalog=catalog, title="Notes on the Analytical Engine")

    def _request(self):
        request = RequestFactory().post("/assistant/v1/chats")
        request.user = self.reader
        return request

    def _turn(self, rounds, chat=None):
        chat = chat or Chat.objects.create(user=self.reader)
        with mock.patch.object(turn, "make_client", return_value=ScriptedClient(rounds)):
            frames = list(turn.run(self._request(), chat, "Máte niečo o Bernoulliho číslach?", language="sk"))
        return chat, evaluation.parse_frames(frames)

    def _search(self, *pages):
        """A `search_passages` stand-in returning one readable passage per page of the public entry."""
        hits = [
            SimpleNamespace(
                entry_id=self.entry.pk,
                passages=[
                    SimpleNamespace(paragraph_id=uuid4(), page=page, section="Notes", score=0.9, text=f"Text {page}")
                    for page in pages
                ],
            )
        ]
        return mock.patch(
            "apps.extraction.discovery.search_passages",
            return_value=SimpleNamespace(degraded=False, items=hits, indexed=1),
        )

    def test_answer_citations_are_stored_and_streamed(self):
        with self._search(12, 13):
            chat, events = self._turn(
                [[_call(tools.SEARCH_PASSAGES, query="Bernoulli numbers")], "Lovelace computes them [E2] [E5]."]
            )
        frames = dict(events)
        self.assertEqual([c["label"] for c in frames["citations"]["citations"]], ["E2"])
        self.assertEqual(frames["citations"]["citations"][0]["page"], 13)
        self.assertEqual(frames["citations"]["citations"][0]["entry_id"], str(self.entry.pk))
        self.assertEqual(frames["citations"]["unknown"], ["E5"])

        answer = chat.messages.visible().filter(role=ChatMessage.Role.ASSISTANT).get()
        self.assertEqual(answer.text, "Lovelace computes them [E2] [E5].")  # stored as streamed
        self.assertEqual(answer.citations[0]["title"], self.entry.title)

    def test_labels_continue_across_turns(self):
        chat = Chat.objects.create(user=self.reader)
        with self._search(1, 2):
            self._turn([[_call(tools.SEARCH_PASSAGES, query="first")], "See [E1]."], chat)
        with self._search(3):
            _chat, events = self._turn([[_call(tools.SEARCH_PASSAGES, query="second")], "See [E3] and [E1]."], chat)

        seen = json.loads(
            chat.messages.filter(role=ChatMessage.Role.TOOL, tool_result__has_key="evidence").last().text
        )
        self.assertEqual(seen["items"][0]["passages"][0]["label"], "E3")
        citations = dict(events)["citations"]
        self.assertEqual([c["page"] for c in citations["citations"]], [3, 1])
        self.assertEqual(citations["unknown"], [])

    def test_answer_without_labels_sends_no_citations_frame(self):
        _chat, events = self._turn(["Dobrý deň, s čím vám pomôžem?"])
        self.assertNotIn("citations", [event for event, _data in events])

    def test_evaluation_fails_an_invented_label(self):
        outcome = evaluation.Outcome(id="x", question="q", citations=1, unknown_labels=["E9"])
        scored = evaluation.score({"expect": {"cites": True}}, outcome)
        self.assertFalse(scored.passed)
        self.assertIn("cited unknown labels E9", scored.failures)

    def test_evaluation_checks_cited_entries(self):
        outcome = evaluation.Outcome(id="x", question="q", citations=1, cited_entries=["a"])
        self.assertTrue(evaluation.score({"expect": {"cites": True, "cites_entries": ["a"]}}, outcome).passed)
        outcome = evaluation.Outcome(id="x", question="q")
        scored = evaluation.score({"expect": {"cites": True, "cites_entries": ["a"]}}, outcome)
        self.assertEqual(scored.failures, ["no citation", "not cited: a"])
