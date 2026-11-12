"""A whole turn against the database with a scripted model (IP-022), and the evaluation harness."""

import json
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.assistant import evaluation, tools, turn
from apps.assistant.llm import Delta
from apps.assistant.models import Chat, ChatMessage
from apps.core.models import AuthSource, Catalog, Category, Entry, User


class ScriptedClient:
    """Answers each round from a list: tool calls, or text. Records what it was sent."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []

    def chat(self, messages, tools=None, response_format=None, span_attributes=None):
        self.requests.append({"messages": messages, "tools": tools, "span_attributes": span_attributes})
        step = self.rounds.pop(0)
        if isinstance(step, str):
            yield Delta(content=step)
        else:
            yield Delta(tool_calls=step)
        yield Delta(done=True, prompt_tokens=100, completion_tokens=10)


def _call(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


class TurnTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = User(username="reader", name="Reader", surname="Tester", auth_source=source)
        cls.reader.set_unusable_password()
        cls.reader.save()
        cls.public = Catalog.objects.create(creator=cls.reader, title="Open", url_name="open", is_public=True)
        cls.hidden = Catalog.objects.create(creator=cls.reader, title="Hidden", url_name="hidden")
        cls.entry = Entry.objects.create(
            creator=cls.reader, catalog=cls.public, title="Notes on the Analytical Engine", summary="Lovelace " * 100
        )
        cls.secret = Entry.objects.create(creator=cls.reader, catalog=cls.hidden, title="Confidential Ledger")

    def _request(self):
        request = RequestFactory().post("/assistant/v1/chats")
        request.user = self.reader
        return request

    def _turn(self, rounds, text="Máte niečo o Analytical Engine?"):
        client = ScriptedClient(rounds)
        chat = Chat.objects.create(user=self.reader)
        with mock.patch.object(turn, "OllamaClient", return_value=client):
            frames = list(turn.run(self._request(), chat, text, language="sk"))
        return chat, client, evaluation.parse_frames(frames)

    def test_search_then_display_closes_the_turn_with_slim_results_for_the_model(self):
        chat, client, events = self._turn(
            [
                [_call("search_entries", query="Analytical", author=None, language_codes=[])],
                [_call(tools.DISPLAY_BOOKS, entry_ids=[str(self.entry.pk)])],
            ]
        )
        names = [event for event, _data in events]
        self.assertIn("status", names)
        self.assertIn("entries", names)
        self.assertNotIn("error", names)

        tool_row = chat.messages.filter(role=ChatMessage.Role.TOOL).exclude(text="").first()
        seen = json.loads(tool_row.text)
        self.assertEqual(seen["items"][0]["id"], str(self.entry.pk))
        self.assertNotIn("resource_uri", seen["items"][0])
        self.assertIn("resource_uri", tool_row.tool_result["items"][0])

        # The second round replayed the slim result, and the tool list did not change.
        self.assertEqual(client.requests[0]["tools"], client.requests[1]["tools"])
        self.assertIn(tool_row.text, [m["content"] for m in client.requests[1]["messages"]])

    def test_find_similar_refuses_an_entry_the_reader_cannot_see(self):
        request = RequestFactory().post("/")
        request.user = AnonymousUser()  # sees public catalogs only
        payload, text = tools.run(request, tools.FIND_SIMILAR, {"entry_id": str(self.secret.pk)})
        self.assertIsNone(payload)
        self.assertIn("error", json.loads(text))

    def test_find_similar_without_vectors_is_an_empty_answer(self):
        payload, text = tools.run(self._request(), tools.FIND_SIMILAR, {"entry_id": str(self.entry.pk)})
        self.assertEqual(payload["items"], [])
        self.assertIn("note", json.loads(text))

    def test_search_passages_without_embeddings_says_so(self):
        with mock.patch("apps.core.search.embeddings.try_embed_query", return_value=None):
            payload, text = tools.run(self._request(), tools.SEARCH_PASSAGES, {"query": "Bernoulli numbers"})
        self.assertEqual(payload["items"], [])
        self.assertIn("unavailable", json.loads(text)["note"])

    def test_list_categories_returns_the_whole_vocabulary(self):
        # More terms than one MCP page (50): STU has 59.
        for number in range(60):
            Category.objects.create(creator=self.reader, catalog=self.public, term=f"{number:03d}", label={})
        payload, text = tools.run(self._request(), "list_categories", {})
        seen = json.loads(text)
        self.assertEqual(len(payload["items"]), 60)
        self.assertEqual(seen["total"], 60)
        self.assertEqual(len(seen["items"]), 60)

    def test_evaluation_case_runs_a_real_turn_and_deletes_its_chat(self):
        client = ScriptedClient(
            [
                [_call("search_entries", query="Analytical")],
                [_call(tools.DISPLAY_BOOKS, entry_ids=[str(self.entry.pk)])],
            ]
        )
        case = {
            "id": "sk-topic",
            "language": "sk",
            "question": "Máte niečo o Analytical Engine?",
            "expect": {"tools_any": ["search_entries"], "displays": True},
        }
        with mock.patch.object(turn, "OllamaClient", return_value=client):
            outcome = evaluation.run_case(self.reader, case)
        self.assertTrue(outcome.passed, outcome.failures)
        self.assertEqual(outcome.tools, ["search_entries", "displayBooks"])
        self.assertEqual(outcome.rounds, 2)
        self.assertFalse(Chat.objects.filter(title__startswith="[evaluation]").exists())


class LanguageDetectionTests(SimpleTestCase):
    def test_slovak_czech_english(self):
        self.assertEqual(evaluation.answer_language("Tieto knihy si môžete požičať hneď teraz."), "sk")
        self.assertEqual(evaluation.answer_language("Tyto knihy si můžete půjčit hned teď, jsou dostupné."), "cs")
        self.assertEqual(evaluation.answer_language("These books can be borrowed right now."), "en")
        self.assertIsNone(evaluation.answer_language("OK"))

    def test_score_flags_czech_and_missing_tools(self):
        outcome = evaluation.Outcome(id="x", question="q", tools=["get_entry"], answer_language="cs")
        scored = evaluation.score({"language": "sk", "expect": {"tools_any": ["search_entries"]}}, outcome)
        self.assertFalse(scored.passed)
        self.assertEqual(len(scored.failures), 2)
