"""Chapter summaries (#119) and the cached prompt prefix (#114)."""

import json
from unittest import mock

import fitz
from django.core.files.base import ContentFile
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.assistant import chapters, tools, turn
from apps.assistant.llm import Delta
from apps.assistant.models import ChapterSummary, Chat, ChatMessage
from apps.assistant.tests.test_turn_tools import ScriptedClient, _call
from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User

OUTLINE = [
    {"level": 1, "title": "Introduction", "page": 1},
    {"level": 2, "title": "Scope", "page": 2},
    {"level": 1, "title": "Fluids", "page": 3},
    {"level": 1, "title": "Index", "page": 5},
]


class ChapterListTests(SimpleTestCase):
    def test_top_level_chapters_run_until_the_next(self):
        found = chapters.chapters(OUTLINE, page_count=6)
        self.assertEqual(
            [(c.title, c.page, c.end_page) for c in found], [("Introduction", 1, 2), ("Fluids", 3, 4), ("Index", 5, 6)]
        )

    def test_a_single_book_title_heading_descends_a_level(self):
        outline = [{"level": 1, "title": "The Book", "page": 1}] + [
            {"level": 2, "title": f"Chapter {n}", "page": n * 10} for n in range(1, 4)
        ]
        self.assertEqual([c.title for c in chapters.chapters(outline, 40)], ["Chapter 1", "Chapter 2", "Chapter 3"])

    def test_no_outline(self):
        self.assertEqual(chapters.chapters(None, 10), [])


class Summariser:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, response_format=None):
        self.calls += 1
        yield Delta(content=json.dumps({"summary": f"Summary {self.calls} of {messages[1]['content'][:20]}"}))
        yield Delta(done=True, prompt_tokens=50, completion_tokens=20)


def _pdf(pages):
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10)
    data = document.tobytes()
    document.close()
    return data


class SummariseEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="librarian", name="L", surname="T", auth_source=source)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.catalog = Catalog.objects.create(creator=cls.user, title="Open", url_name="open", is_public=True)

    def _entry(self, **config):
        entry = Entry.objects.create(
            creator=self.user, catalog=self.catalog, title="Fluids", table_of_contents=OUTLINE, page_count=6
        )
        if config:
            entry.config = config
            entry.save()
        acquisition = Acquisition(entry=entry, mime=Acquisition.AcquisitionMIME.PDF)
        long = "word " * 120
        acquisition.content.save(
            "f.pdf", ContentFile(_pdf([f"intro {long}", "scope", f"fluids {long}", "more", "", ""]))
        )
        return entry

    def test_summaries_are_stored_once_and_kept_while_the_text_is_the_same(self):
        entry = self._entry()
        client = Summariser()
        # Reader pages = PDF pages (no license page inserted) keeps the outline's numbers readable here.
        with mock.patch("apps.assistant.pdf.reader_source_pages", side_effect=lambda n, r, m: list(range(1, n + 1))):
            first = chapters.summarise_entry(entry.pk, client=client)
            second = chapters.summarise_entry(entry.pk, client=client)
        self.assertEqual(first["summarised"], 2, first)  # "Index" has no text
        self.assertEqual(second["kept"], 2)
        self.assertEqual(client.calls, 2)
        self.assertEqual(
            list(ChapterSummary.objects.filter(entry=entry).values_list("title", flat=True)),
            ["Introduction", "Fluids"],
        )

    def test_lcp_publications_are_never_summarised(self):
        entry = self._entry(readium_enabled=True)
        ChapterSummary.objects.create(entry=entry, title="Old", page=1, end_page=2, summary="leak", digest="x")
        client = Summariser()
        report = chapters.summarise_entry(entry.pk, client=client)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(client.calls, 0)
        self.assertFalse(ChapterSummary.objects.filter(entry=entry).exists())

    def test_get_entry_shows_summarised_chapters_instead_of_the_outline(self):
        entry = self._entry()
        request = RequestFactory().get("/")
        request.user = self.user

        _payload, text = tools.run(request, "get_entry", {"entry_id": str(entry.pk)})
        self.assertEqual(json.loads(text)["entry"]["table_of_contents"][0]["title"], "Introduction")

        ChapterSummary.objects.create(
            entry=entry, title="Fluids", page=3, end_page=4, summary="About flow.", digest="d"
        )
        _payload, text = tools.run(request, "get_entry", {"entry_id": str(entry.pk)})
        seen = json.loads(text)["entry"]
        self.assertNotIn("table_of_contents", seen)
        self.assertEqual(seen["chapters"], [{"title": "Fluids", "page": 3, "end_page": 4, "summary": "About flow."}])
        self.assertIn("unchecked", seen["chapters_note"])


class PromptPrefixTests(TestCase):
    """#114: the system prompt and tool list are byte-identical across rounds, turns and chats."""

    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = User(username="reader", name="R", surname="T", auth_source=source)
        cls.reader.set_unusable_password()
        cls.reader.save()

    def _requests(self, chat, rounds, language):
        client = ScriptedClient(rounds)
        request = RequestFactory().post("/")
        request.user = self.reader
        with mock.patch.object(turn, "OllamaClient", return_value=client):
            list(turn.run(request, chat, "Máte niečo o tekutinách?", language=language))
        return client.requests

    def test_prefix_is_identical_across_rounds_turns_and_chats(self):
        first_chat = Chat.objects.create(user=self.reader)
        second_chat = Chat.objects.create(user=self.reader)
        requests = (
            self._requests(first_chat, [[_call("list_categories")], "Nič."], "sk")
            + self._requests(first_chat, ["Ešte raz nič."], "en")
            + self._requests(second_chat, ["Ahoj."], "sk")
        )
        heads = {json.dumps([request["messages"][0], request["tools"]], ensure_ascii=False) for request in requests}
        self.assertEqual(len(heads), 1)
        # The reader's language is the last message, never part of the prefix.
        self.assertEqual(requests[0]["messages"][-1]["role"], "system")
        # Every assistant row records which prompt produced it.
        fingerprints = set(
            ChatMessage.objects.filter(role=ChatMessage.Role.ASSISTANT).values_list("prompt_fingerprint", flat=True)
        )
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(len(fingerprints.pop()), 12)
