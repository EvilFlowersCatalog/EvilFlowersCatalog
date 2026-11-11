"""Reading publications from a plain chat (#127) and the round cap (#130), against the database."""

import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.assistant import evaluation, tools, turn
from apps.assistant.models import Chat
from apps.assistant.pdf import DocumentUnavailable, ReaderDocument
from apps.assistant.tests.test_turn_tools import ScriptedClient, _call
from apps.core.models import AuthSource, Catalog, Entry, User

BOOK = ReaderDocument(
    [
        "Preface.",
        "The Analytical Engine computes Bernoulli numbers with a sequence of operations.",
        "Note G gives the table of operations.",
    ],
    toc=[],
)


def _open(document=BOOK):
    return mock.patch("apps.assistant.pdf.open_access_document", return_value=document)


class ReadPublicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = User(username="reader", name="Reader", surname="Tester", auth_source=source)
        cls.reader.set_unusable_password()
        cls.reader.save()
        cls.public = Catalog.objects.create(creator=cls.reader, title="Open", url_name="open", is_public=True)
        cls.hidden = Catalog.objects.create(creator=cls.reader, title="Hidden", url_name="hidden")
        cls.entry = Entry.objects.create(creator=cls.reader, catalog=cls.public, title="Notes on the Engine")
        cls.other = Entry.objects.create(creator=cls.reader, catalog=cls.public, title="Sketch of the Engine")
        cls.lent = Entry.objects.create(
            creator=cls.reader, catalog=cls.public, title="Lent Book", config={"readium_enabled": True}
        )
        cls.secret = Entry.objects.create(creator=cls.reader, catalog=cls.hidden, title="Confidential Ledger")

    def _request(self, user=None):
        request = RequestFactory().post("/assistant/v1/chats")
        request.user = user or self.reader
        return request

    def _turn(self, rounds, text="Čo píše kniha o Bernoulliho číslach?"):
        client = ScriptedClient(rounds)
        chat = Chat.objects.create(user=self.reader)
        with mock.patch.object(turn, "OllamaClient", return_value=client):
            frames = list(turn.run(self._request(), chat, text, language="sk"))
        return chat, client, evaluation.parse_frames(frames)

    def test_a_page_read_in_a_plain_chat_is_cited_like_a_passage(self):
        with _open():
            chat, client, events = self._turn(
                [[_call(tools.READ_PAGE, entry_id=str(self.entry.pk), page=2)], "Počíta Bernoulliho čísla [E1]."]
            )
        seen = json.loads(client.requests[1]["messages"][-2]["content"])
        self.assertEqual(seen["pages"][0]["label"], "E1")
        self.assertNotIn("evidence", seen)
        citations = dict(events)["citations"]["citations"]
        self.assertEqual(citations[0]["entry_id"], str(self.entry.pk))
        self.assertEqual(citations[0]["page"], 2)
        # Pages of a publication the chat is not bound to are not viewer pages.
        self.assertNotIn("pages", dict(events))

    def test_a_page_read_again_keeps_its_label(self):
        ledger = tools_ledger()
        with _open():
            first, _text = tools.run(
                self._request(), tools.READ_PAGE, {"entry_id": str(self.entry.pk), "page": 2}, ledger
            )
            again, _text = tools.run(
                self._request(), tools.READ_PAGE, {"entry_id": str(self.entry.pk), "page": 2, "count": 2}, ledger
            )
        self.assertEqual(first["pages"][0]["label"], again["pages"][0]["label"])
        self.assertEqual(again["pages"][1]["label"], "E2")

    def test_readium_publications_are_refused_with_the_borrow_hint(self):
        with mock.patch("apps.assistant.pdf.open_access_document", side_effect=DocumentUnavailable("readium")):
            payload, text = tools.run(self._request(), tools.READ_PAGE, {"entry_id": str(self.lent.pk), "page": 1})
        self.assertIsNone(payload)
        self.assertIn("Readium LCP", json.loads(text)["error"])
        self.assertIn("borrow", json.loads(text)["error"])

    def test_the_real_open_access_rule_refuses_readium(self):
        payload, text = tools.run(self._request(), tools.READ_PAGE, {"entry_id": str(self.lent.pk), "page": 1})
        self.assertIsNone(payload)
        self.assertIn("Readium LCP", json.loads(text)["error"])

    def test_a_publication_the_reader_cannot_see_is_not_read(self):
        with _open() as opened:
            payload, text = tools.run(
                self._request(AnonymousUser()), tools.READ_PAGE, {"entry_id": str(self.secret.pk), "page": 1}
            )
        self.assertIsNone(payload)
        self.assertIn("No publication", json.loads(text)["error"])
        opened.assert_not_called()

    @override_settings(EVILFLOWERS_ASSISTANT_MAX_PAGES_READ=3)
    def test_one_page_budget_across_publications(self):
        reading = tools.Reading()
        with _open():
            tools.run(
                self._request(),
                tools.READ_PAGE,
                {"entry_id": str(self.entry.pk), "page": 1, "count": 3},
                None,
                reading,
            )
            payload, text = tools.run(
                self._request(), tools.READ_PAGE, {"entry_id": str(self.other.pk), "page": 1}, None, reading
            )
        self.assertIsNone(payload)
        self.assertIn("budget", json.loads(text)["error"])

    def test_search_inside_one_publication_falls_back_to_word_matches(self):
        with _open(), mock.patch("apps.core.search.embeddings.try_embed_query", return_value=None):
            payload, text = tools.run(
                self._request(), tools.SEARCH_PASSAGES, {"query": "Bernoulli numbers", "entry_id": str(self.entry.pk)}
            )
        seen = json.loads(text)
        self.assertEqual(seen["matches"][0]["page"], 2)
        self.assertIn("read_pdf_page", seen["note"])

    def test_a_plain_chat_ends_with_an_answer_round_without_tools(self):
        searches = [[_call("search_entries", query=f"engine {number}")] for number in range(turn.MAX_CHAT_ROUNDS - 1)]
        chat, client, events = self._turn(searches + ["Nič vhodné som nenašla."], text="Máte niečo o motoroch?")
        self.assertEqual(len(client.requests), turn.MAX_CHAT_ROUNDS)
        self.assertIsNone(client.requests[-1]["tools"])
        self.assertEqual(client.requests[-1]["messages"][-1]["content"], turn.ANSWER_NOW)
        self.assertEqual(dict(events)["message"]["text"], "Nič vhodné som nenašla.")
        self.assertEqual(chat.messages.order_by("-created_at").first().text, "Nič vhodné som nenašla.")

    def test_a_silent_final_round_still_answers(self):
        searches = [[_call("search_entries", query=f"engine {number}")] for number in range(turn.MAX_CHAT_ROUNDS - 1)]
        _chat, _client, events = self._turn(searches + [""], text="Máte niečo o motoroch?")
        self.assertIn("rephrase", dict(events)["message"]["text"])

    def test_a_bound_case_runs_with_the_publication_open(self):
        client = ScriptedClient(
            [[_call("read_pdf_page", page=2)], "Analytický stroj počíta Bernoulliho čísla [[page:2]]."]
        )
        case = {
            "id": "sk-bound",
            "language": "sk",
            "entry_title": "Notes on the Engine",
            "question": "Čo počíta stroj?",
            "expect": {"grounded": True, "reads_pages": True},
        }
        with mock.patch.object(turn, "OllamaClient", return_value=client), _open():
            outcome = evaluation.run_case(self.reader, case)
        self.assertTrue(outcome.passed, outcome.failures)
        self.assertEqual(outcome.pages_read, 1)

    def test_a_case_about_a_missing_publication_is_an_error_not_a_crash(self):
        outcome = evaluation.run_case(self.reader, {"id": "x", "question": "q", "entry_title": "No Such Book"})
        self.assertFalse(outcome.passed)
        self.assertIn("No visible publication", outcome.error)

    def test_search_inside_one_publication_is_scoped_to_it(self):
        # `search_passages` ranks only paragraphs of `EntryFilter(scope)`; `id` must narrow it to the one entry,
        # or `search_library_text(entry_id)` would quietly search the whole library.
        from apps.api.filters.entries import EntryFilter
        from apps.extraction import discovery

        scope = {"id": str(self.entry.pk)}
        visible = EntryFilter(scope, queryset=Entry.objects.all(), request=self._request()).qs
        self.assertEqual(list(visible.values_list("pk", flat=True)), [self.entry.pk])

        with mock.patch.object(discovery, "search_passages", wraps=discovery.search_passages) as search, _open():
            tools.run(self._request(), tools.SEARCH_PASSAGES, {"query": "Bernoulli", "entry_id": str(self.entry.pk)})
        self.assertEqual(search.call_args.kwargs["scope"], scope)

    @override_settings(EVILFLOWERS_ASSISTANT_MAX_PAGES_READ=3)
    def test_pages_read_by_id_count_against_a_bound_chats_budget(self):
        reading = tools.Reading([])
        with _open():
            tools.run(
                self._request(),
                tools.READ_PAGE,
                {"entry_id": str(self.other.pk), "page": 1, "count": 3},
                None,
                reading,
            )
        chat = SimpleNamespace(user=self.reader, user_acquisition_id=None)
        call = _call("read_pdf_page", page=1)
        with mock.patch.object(turn.ChatMessage.objects, "create") as create:
            list(turn._run_tool(self._request(), chat, call, BOOK, reading.bound, reading=reading))
        self.assertIn("budget", create.call_args.kwargs["text"])


def tools_ledger():
    from apps.assistant.evidence import Ledger

    return Ledger()


class BoundChatRoutingTests(SimpleTestCase):
    def test_read_pdf_page_for_another_publication_leaves_the_readers_copy(self):
        document = SimpleNamespace(acquisition=SimpleNamespace(entry_id="a"))
        self.assertTrue(turn._another_publication(document, {"entry_id": "b", "page": 1}))
        self.assertFalse(turn._another_publication(document, {"entry_id": "a", "page": 1}))
        self.assertFalse(turn._another_publication(document, {"page": 1}))

    def test_other_document_tools_point_to_the_plain_chat_tools(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
        _payload, text = tools.run(request, "search_pdf", {"entry_id": "b", "query": "x"})
        self.assertIn(tools.SEARCH_PASSAGES, json.loads(text)["error"])


class GroundingScoreTests(SimpleTestCase):
    def test_content_without_reading_fails_a_grounded_case(self):
        outcome = evaluation.Outcome(id="x", question="q", tools=["search_entries"], answer_language="sk")
        scored = evaluation.score({"language": "sk", "expect": {"grounded": True}}, outcome)
        self.assertIn("content answered without reading any publication", scored.failures)

    def test_reading_passes_a_grounded_case(self):
        outcome = evaluation.Outcome(id="x", question="q", tools=["search_library_text"], answer_language="sk")
        self.assertTrue(evaluation.score({"language": "sk", "expect": {"grounded": True}}, outcome).passed)

    def test_a_single_czech_word_fails_a_slovak_answer(self):
        answer = "Knihovna má tieto knihy o tabaku, ktoré si môžete požičať."
        self.assertEqual(evaluation.answer_language(answer), "sk")
        self.assertEqual(evaluation.czech_words(answer), ["knihovna"])
        outcome = evaluation.Outcome(
            id="x", question="q", answer=answer, answer_language="sk", czech_words=evaluation.czech_words(answer)
        )
        self.assertIn("Czech words: knihovna", evaluation.score({"language": "sk"}, outcome).failures)

    def test_slovak_words_are_not_czech(self):
        self.assertEqual(evaluation.czech_words("Knižnica má také knihy, ktoré si môžete požičať, a tiež ďalšie."), [])
