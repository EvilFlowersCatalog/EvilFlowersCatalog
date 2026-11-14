"""`structured.parse_object` and the explain retry (IP-024 §2)."""

from unittest import mock

from django.test import SimpleTestCase

from apps.assistant import explain, highlight, semantic, structured
from apps.assistant.llm import Delta


def _simple(data):
    return isinstance(data.get("simple"), str)


class ParseObjectTests(SimpleTestCase):
    def test_plain_object(self):
        self.assertEqual(structured.parse_object('{"simple": "x"}'), {"simple": "x"})

    def test_fenced_block_with_prose_around_it(self):
        content = 'Here you go:\n```json\n{"simple": "x", "examples": []}\n```\nHope it helps {sic}.'
        self.assertEqual(structured.parse_object(content, _simple)["simple"], "x")

    def test_first_valid_object_wins_over_an_earlier_invalid_one(self):
        content = 'I read {"page": 3}. Answer: {"simple": "y", "examples": []}'
        self.assertEqual(structured.parse_object(content, _simple)["simple"], "y")

    def test_trailing_comma_and_typographic_quotes_are_repaired(self):
        content = "{“simple”: “z”, “examples”: [],}"
        self.assertEqual(structured.parse_object(content, _simple), {"simple": "z", "examples": []})

    def test_valid_text_is_not_repaired(self):
        # Slovak quotation marks inside a string survive when the object already decodes.
        content = '{"simple": "Pojem „entropia“ znamená…"}'
        self.assertEqual(structured.parse_object(content, _simple)["simple"], "Pojem „entropia“ znamená…")

    def test_trailing_comma_repair_keeps_slovak_quotation_marks(self):
        content = '{"simple": "Pojem „entropia“ znamená…", "examples": [],}'
        self.assertEqual(structured.parse_object(content, _simple)["simple"], "Pojem „entropia“ znamená…")

    def test_nothing_usable(self):
        self.assertIsNone(structured.parse_object("No JSON here.", _simple))
        self.assertIsNone(structured.parse_object(None))
        self.assertIsNone(structured.parse_object('{"other": 1}', _simple))

    def test_callers_share_it(self):
        self.assertEqual(highlight._json('```json\n{"quotes": [], "summary": "s",}\n```')["summary"], "s")
        self.assertEqual(semantic._json("nothing"), {})


class ScriptedClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def chat(self, messages, tools=None, response_format=None):
        self.requests.append({"tools": tools, "format": response_format, "messages": list(messages)})
        yield Delta(content=self.answers.pop(0))
        yield Delta(done=True, prompt_tokens=10, completion_tokens=5)


class Document:
    page_count = 3

    def page(self, number):
        return "Entropy is a measure of disorder." if number == 1 else None

    def section_of(self, number):
        return []


class ExplainRetryTests(SimpleTestCase):
    def _explain(self, answers):
        client = ScriptedClient(answers)
        with mock.patch.object(explain, "make_client", return_value=client):
            result = explain.explain(Document(), text="entropy", page=1, title="Physics", authors="", language="sk")
        return result, client

    def test_json_answer_needs_no_retry(self):
        result, client = self._explain(['{"simple": "Miera neusporiadanosti.", "examples": []}'])
        self.assertEqual(result.simple, "Miera neusporiadanosti.")
        self.assertEqual(len(client.requests), 1)

    def test_prose_is_retried_once_constrained_to_the_schema(self):
        result, client = self._explain(
            ["Entropia je miera neusporiadanosti.", '{"simple": "Miera neusporiadanosti.", "examples": []}']
        )
        self.assertEqual(result.simple, "Miera neusporiadanosti.")
        self.assertEqual(len(client.requests), 2)
        retry = client.requests[1]
        self.assertIsNone(retry["tools"])
        self.assertEqual(retry["format"], explain.ANSWER_SCHEMA)
        self.assertIn(structured.RETRY_INSTRUCTION, retry["messages"][-1]["content"])
        self.assertEqual(result.tokens_used, 30)

    def test_failed_retry_falls_back_to_the_first_prose(self):
        result, client = self._explain(["Entropia je miera neusporiadanosti.", "stále nie JSON"])
        self.assertEqual(result.simple, "Entropia je miera neusporiadanosti.")
        self.assertEqual(len(client.requests), 2)
