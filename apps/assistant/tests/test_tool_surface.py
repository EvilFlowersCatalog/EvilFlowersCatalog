"""What the model is offered and what it sees of results (IP-022), without a database."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

import apps.mcp.tools  # noqa: F401  (fills the registry)
from apps.assistant import services, tools, turn
from apps.mcp.errors import ToolError
from apps.mcp.registry import registry


def _names(specs):
    return [spec["function"]["name"] for spec in specs]


class OfferedToolsTests(SimpleTestCase):
    def test_every_allowed_registry_tool_is_read_only(self):
        # ALLOWED_TOOLS is the security boundary (IP-015 D1): never a writing tool.
        for name in tools.ALLOWED_TOOLS:
            tool = registry.get(name)
            self.assertIsNotNone(tool, name)
            self.assertFalse(tool.writes, name)

    def test_every_offered_tool_has_a_specification(self):
        for name in tools._offered(authenticated=True):
            self.assertIn(name, tools.SPECIFICATIONS)

    def test_reader_tools_only_for_authenticated_sessions(self):
        anonymous = _names(tools.specifications(authenticated=False))
        signed_in = _names(tools.specifications(authenticated=True))
        for name in ("get_my_shelf", "list_my_loans"):
            self.assertNotIn(name, anonymous)
            self.assertIn(name, signed_in)
        for name in ("search_entries", "get_entry", tools.FIND_SIMILAR, tools.SEARCH_PASSAGES, tools.DISPLAY_BOOKS):
            self.assertIn(name, anonymous)

    def test_specifications_are_stable(self):
        # Part of the cached prompt prefix: same session, same bytes.
        first = json.dumps(tools.specifications(authenticated=True))
        self.assertEqual(first, json.dumps(tools.specifications(authenticated=True)))

    def test_first_round_prefix_stays_within_the_cpu_budget(self):
        # #104: ≤ 3K tokens for the first round of a plain chat (~4 characters a token).
        chars = len(json.dumps(tools.specifications(authenticated=True))) + len(services.SYSTEM_PROMPT)
        self.assertLess(chars // 4, 2000)

    def test_advertised_search_arguments_are_accepted_by_the_handler(self):
        from apps.mcp.tools.entries import SEARCH_ARGUMENTS

        advertised = tools.SPECIFICATIONS["search_entries"][1]["properties"]
        self.assertLessEqual(set(advertised), set(SEARCH_ARGUMENTS))


class ArgumentTests(SimpleTestCase):
    def test_empty_values_are_dropped(self):
        self.assertEqual(
            tools.clean_arguments({"query": "entropia", "author": None, "language_codes": [], "title": ""}),
            {"query": "entropia"},
        )
        self.assertEqual(tools.clean_arguments("nonsense"), {})

    def test_search_limit_is_clamped(self):
        handler = mock.Mock(return_value={"items": [], "metadata": {"total": 0}})
        fake = SimpleNamespace(name="search_entries", access=registry.get("search_entries").access, handler=handler)
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=False))
        with mock.patch.object(tools.registry, "get", return_value=fake):
            tools.call(request, "search_entries", {"query": "x", "limit": 500})
            self.assertEqual(handler.call_args.args[1]["limit"], tools.MAX_SEARCH_LIMIT)
            tools.call(request, "search_entries", {"query": "x"})
            self.assertEqual(handler.call_args.args[1]["limit"], tools.DEFAULT_SEARCH_LIMIT)

    def test_tool_errors_reach_the_model(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
        with mock.patch.object(tools, "call", side_effect=ToolError("Unknown argument(s): category.")):
            payload, text = tools.run(request, "search_entries", {"category": "x"})
        self.assertIsNone(payload)
        self.assertIn("Unknown argument(s): category.", json.loads(text)["error"])

    def test_reader_tools_refused_when_anonymous(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=False))
        payload, text = tools.run(request, "list_my_loans", {})
        self.assertIsNone(payload)
        self.assertIn("error", json.loads(text))

    def test_writing_tools_are_unknown(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
        _payload, text = tools.run(request, "delete_feed", {"feed_id": "x"})
        self.assertIn("error", json.loads(text))


SEARCH_PAYLOAD = {
    "items": [
        {
            "id": "7f1c3c9e-0000-4000-8000-000000000001",
            "catalog_id": "c",
            "title": "Information Theory",
            "authors": ["C. Shannon"],
            "published_at": "1949-01-01",
            "publisher": "UIP",
            "language": "en",
            "categories": ["004"],
            "summary": "word " * 200,
            "summary_truncated": True,
            "page_count": 120,
            "popularity": 7,
            "availability": {"state": "fully_borrowed", "available_slots": 0, "total_slots": 1, "package_ready": True},
            "resource_uri": "evilflowers://entries/x",
        }
    ],
    "metadata": {"page": 1, "limit": 6, "pages": 1, "total": 1, "has_next_page": False},
}


class SlimTests(SimpleTestCase):
    def test_search_hits_keep_what_the_model_reasons_with(self):
        slim = tools.slim("search_entries", SEARCH_PAYLOAD)
        item = slim["items"][0]
        self.assertEqual(slim["total"], 1)
        self.assertEqual(item["year"], "1949")
        self.assertEqual(item["availability"], {"state": "fully_borrowed", "available_slots": 0, "total_slots": 1})
        for dropped in ("catalog_id", "resource_uri", "publisher", "page_count", "popularity", "published_at"):
            self.assertNotIn(dropped, item)
        self.assertLessEqual(len(item["summary"]), tools.SLIM_SUMMARY_CHARS + 1)
        self.assertLess(len(json.dumps(slim)), len(json.dumps(SEARCH_PAYLOAD)) / 2)

    def test_empty_search_tells_the_model_what_to_try(self):
        slim = tools.slim("search_entries", {"items": [], "metadata": {"total": 0}})
        self.assertIn("note", slim)

    def test_loans(self):
        payload = {
            "items": [
                {
                    "id": "l",
                    "entry_id": "e",
                    "entry_title": "T",
                    "state": "active",
                    "expires_at": "2026-12-01T00:00:00Z",
                    "is_expired": False,
                }
            ]
        }
        self.assertEqual(
            tools.slim("list_my_loans", payload)["items"][0],
            {"entry_id": "e", "entry_title": "T", "state": "active", "expires_at": "2026-12-01T00:00:00Z"},
        )


class RepeatedCallTests(SimpleTestCase):
    def test_a_repeated_call_is_answered_with_a_hint_not_run(self):
        chat = SimpleNamespace(user=object())
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
        call = {"function": {"name": "search_entries", "arguments": {"query": "entropia", "author": None}}}
        repeat = {"function": {"name": "search_entries", "arguments": {"query": "entropia"}}}
        seen = set()

        with (
            mock.patch.object(turn.ChatMessage.objects, "create") as create,
            mock.patch.object(turn.tools, "run", return_value=({}, "{}")) as run,
        ):
            frames = list(_drain(turn._run_tool(request, chat, call, seen_calls=seen)))
            self.assertTrue(any("event: status" in frame for frame in frames))
            list(_drain(turn._run_tool(request, chat, repeat, seen_calls=seen)))

        self.assertEqual(run.call_count, 1)
        self.assertIn("already called", create.call_args.kwargs["text"])


def _drain(generator):
    """Yield a generator's frames, discarding its return value."""
    result = yield from generator
    return result
