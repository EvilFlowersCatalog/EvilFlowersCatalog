"""IP-030 §1, §4, §6: follow, study, library_overview and the evidence pack, against a processed publication."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant import evidence, pack, study, tools
from apps.assistant.library import ToolContext
from apps.assistant.llm import Delta
from apps.assistant.models import SectionSummary
from apps.assistant.tests import fixtures
from apps.extraction import references
from apps.extraction.models import ParagraphLink


class JsonClient:
    """Answers every request with the next JSON payload; records the requests."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []
        self.model = "test-model"

    def chat(self, messages, tools=None, response_format=None, span_attributes=None, max_tokens=None):
        self.requests.append({"messages": messages, "format": response_format, "max_tokens": max_tokens})
        payload = self.payloads.pop(0)
        yield Delta(content=payload if isinstance(payload, str) else json.dumps(payload))
        yield Delta(done=True, prompt_tokens=10, completion_tokens=5, prompt_seconds=1.0, generation_seconds=0.5)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.hidden = fixtures.catalog(cls.reader, public=False, name="hidden")
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, cls.shear = fixtures.publication(cls.reader, cls.open)
        cls.other, cls.other_run, cls.other_rows, _f, _s = fixtures.publication(
            cls.reader, cls.open, title="Hydraulics"
        )
        cls.secret, _run, cls.secret_rows, _f2, _s2 = fixtures.publication(cls.reader, cls.hidden, title="Secret")
        cls.licensed, _run3, cls.lcp_rows, _f3, _s3 = fixtures.publication(
            cls.reader, cls.open, title="Licensed", readium=True
        )
        references.build(cls.extraction)
        # Similar paragraphs elsewhere: one readable, one hidden from an anonymous reader, one licensed.
        for target, score in ((cls.other_rows[2], 0.9), (cls.secret_rows[2], 0.95), (cls.lcp_rows[2], 0.97)):
            ParagraphLink.objects.create(
                source=cls.paragraphs[2], target=target, kind=ParagraphLink.Kind.SIMILAR, score=score
            )

    def context(self, user=None, **kwargs):
        from django.contrib.auth.models import AnonymousUser

        request = fixtures.request_for(user or self.reader)
        if user == "anonymous":
            request.user = AnonymousUser()
        return ToolContext(request=request, ledger=evidence.Ledger(), reading=tools.Reading(), **kwargs)

    def label(self, context, paragraph, entry=None):
        entry = entry or self.entry
        label, _ = context.ledger.issue(
            {"kind": "passage", "entry_id": str(entry.pk), "page": paragraph.page, "paragraph_id": str(paragraph.pk)}
        )
        return label


class FollowTests(Base):
    def _follow(self, context, label, relation):
        payload, text = tools.run(
            context.request,
            tools.FOLLOW,
            {"label": label, "relation": relation},
            ledger=context.ledger,
            context=context,
        )
        return payload, json.loads(text)

    def test_next_and_previous_are_labelled_into_the_ledger(self):
        context = self.context()
        label = self.label(context, self.paragraphs[2])
        payload, seen = self._follow(context, label, "next")
        self.assertEqual([p["page"] for p in payload["passages"]], [4, 5])
        self.assertEqual([p["label"] for p in seen["passages"]], ["E2", "E3"])
        self.assertNotIn("paragraph_id", seen["passages"][0])
        payload, _seen = self._follow(context, label, "previous")
        self.assertEqual([p["page"] for p in payload["passages"]], [1, 2])

    def test_figure_then_mentions(self):
        context = self.context()
        label = self.label(context, self.paragraphs[3])
        _payload, seen = self._follow(context, label, "figure")
        self.assertEqual(seen["figures"][0]["label"], "F1")
        self.assertIn("Venturiho", seen["figures"][0]["caption"])
        _payload, seen = self._follow(context, "F1", "mentions")
        self.assertIn("Obr. 2.1", seen["passages"][0]["text"])

    def test_similar_elsewhere_keeps_to_what_the_reader_may_see_and_read(self):
        context = self.context(user="anonymous")
        label = self.label(context, self.paragraphs[2])
        payload, _seen = self._follow(context, label, "similar_elsewhere")
        self.assertEqual([p["entry_id"] for p in payload["passages"]], [str(self.other.pk)])

    def test_section_reads_within_the_budget(self):
        context = self.context()
        label = self.label(context, self.paragraphs[2])
        payload, _seen = self._follow(context, label, "section")
        self.assertEqual([p["page"] for p in payload["passages"]], [3, 4, 5])
        self.assertEqual(context.reading.pages_for(str(self.entry.pk)), [3, 4, 5])

    def test_readium_text_is_never_followed(self):
        context = self.context()
        label = self.label(context, self.lcp_rows[2], entry=self.licensed)
        payload, seen = self._follow(context, label, "next")
        self.assertIsNone(payload)
        self.assertIn("Readium", seen["error"])

    def test_wrong_label_or_relation_is_explained(self):
        context = self.context()
        _payload, seen = self._follow(context, "E9", "next")
        self.assertIn("label", seen["error"])
        label = self.label(context, self.paragraphs[0])
        _payload, seen = self._follow(context, label, "mentions")
        self.assertIn("figure", seen["error"])

    def test_related_without_the_graph_says_so(self):
        context = self.context()
        label = self.label(context, self.paragraphs[2])
        with mock.patch("apps.graph.projection.available", return_value=False):
            _payload, seen = self._follow(context, label, "related")
        self.assertIn("not available", seen["note"])


class StudyTests(Base):
    def _study(self, arguments, client, context=None):
        context = context or self.context()
        with mock.patch("apps.assistant.llm.client", return_value=client):
            payload, text = tools.run(context.request, tools.STUDY, arguments, ledger=context.ledger, context=context)
        return context, payload, json.loads(text)

    def test_outline_lists_sections_without_a_model(self):
        _context, payload, seen = self._study({"mode": "outline", "entry_id": str(self.entry.pk)}, JsonClient())
        self.assertEqual([s["section"] for s in seen["sections"]], ["1 Introduction", "2 Bernoulli"])
        self.assertEqual(seen["sections"][1]["pages"], "3-5")

    def test_walkthrough_is_a_card_with_citations_and_a_next_step(self):
        client = JsonClient(
            {
                "points": [
                    {"text": "Pressure falls where speed rises.", "labels": ["E1"]},
                    {"text": "A Venturi tube shows it.", "labels": ["E2"]},
                    {"text": "Invented.", "labels": ["E9"]},
                    {"text": "No label.", "labels": []},
                ]
            }
        )
        context, payload, seen = self._study(
            {"mode": "walkthrough", "entry_id": str(self.entry.pk), "section": "bernoulli"}, client
        )
        card = payload["study"]["data"]
        self.assertEqual(card["section"], "2 Bernoulli")
        self.assertEqual([p["labels"] for p in card["points"]], [["E1"], ["E2"]])
        self.assertIsNone(card["next"])  # the last section
        self.assertEqual(context.frames[0][0], "study")
        self.assertEqual(seen["shown"], "walkthrough")
        self.assertIn("[E1]", client.requests[0]["messages"][1]["content"])
        self.assertIsNotNone(client.requests[0]["format"])

    def test_an_artefact_without_valid_labels_is_retried_once_then_refused(self):
        bad = {"questions": [{"question": "?", "options": ["a"], "answer_index": 0, "explanation": "", "labels": []}]}
        client = JsonClient(bad, bad)
        _context, payload, seen = self._study({"mode": "quiz", "entry_id": str(self.entry.pk)}, client)
        self.assertIsNone(payload)
        self.assertIn("could not be prepared", seen["error"])
        self.assertEqual(len(client.requests), 2)

    def test_a_valid_quiz(self):
        question = {"question": "What falls?", "options": ["a", "b", "c", "d"], "answer_index": 1, "explanation": "x"}
        client = JsonClient({"questions": [{**question, "labels": ["E1"]}, {**question, "labels": ["E2"]}]})
        _context, payload, _seen = self._study(
            {"mode": "quiz", "entry_id": str(self.entry.pk), "section": "1 Introduction"}, client
        )
        self.assertEqual(len(payload["study"]["data"]["questions"]), 2)
        self.assertEqual(payload["study"]["data"]["next"] if "next" in payload["study"]["data"] else None, None)

    def test_summary_is_generated_once_and_cached(self):
        long_text = [(page, "2 Bernoulli", "word " * 80) for page in (3, 4)]
        entry, run, _rows, _f, _s = fixtures.publication(self.reader, self.open, title="Long", paragraphs=long_text)
        client = JsonClient({"summary": "About flow.", "concepts": [{"term": "Pressure", "role": "defines"}]})
        arguments = {"mode": "summary", "entry_id": str(entry.pk)}
        _context, _payload, seen = self._study(arguments, client)
        self.assertEqual(seen["summary"], "About flow.")
        self.assertEqual(seen["concepts"], ["Pressure"])
        _context, _payload, seen = self._study(arguments, JsonClient())
        self.assertEqual(seen["summary"], "About flow.")
        self.assertEqual(SectionSummary.objects.get(run=run).concepts, [{"term": "Pressure", "role": "defines"}])

    def test_the_model_time_of_a_study_request_is_booked(self):
        client = JsonClient({"points": [{"text": "a", "labels": ["E1"]}, {"text": "b", "labels": ["E2"]}]})
        context, _payload, _seen = self._study({"mode": "walkthrough", "entry_id": str(self.entry.pk)}, client)
        self.assertEqual(context.model_seconds, 1.5)

    def test_a_busy_model_is_a_tool_error_not_a_failed_turn(self):
        from apps.assistant.llm import LLMUnavailable

        class Busy(JsonClient):
            def chat(self, *args, **kwargs):
                raise LLMUnavailable("every slot is leased")
                yield  # pragma: no cover

        _context, payload, seen = self._study({"mode": "quiz", "entry_id": str(self.entry.pk)}, Busy())
        self.assertIsNone(payload)
        self.assertIn("busy", seen["error"])

    def test_sections_in_get_entry_only_where_the_text_may_be_read(self):
        context = self.context()
        _payload, text = tools.run(context.request, "get_entry", {"entry_id": str(self.entry.pk)})
        self.assertEqual(json.loads(text)["entry"]["sections"][1]["section"], "2 Bernoulli")
        with mock.patch("apps.extraction.discovery._may_read_text", return_value=False):
            _payload, text = tools.run(context.request, "get_entry", {"entry_id": str(self.entry.pk)})
        self.assertNotIn("sections", json.loads(text)["entry"])

    def test_readium_publications_are_refused(self):
        _context, payload, seen = self._study({"mode": "walkthrough", "entry_id": str(self.licensed.pk)}, JsonClient())
        self.assertIsNone(payload)
        self.assertIn("Readium", seen["error"])

    @override_settings(EVILFLOWERS_ASSISTANT_STUDY_MAX_PAGES=1)
    def test_long_sections_come_in_parts(self):
        client = JsonClient(
            {"points": [{"text": "a", "labels": ["E1"]}, {"text": "b", "labels": ["E1"]}]},
        )
        _context, payload, _seen = self._study(
            {"mode": "walkthrough", "entry_id": str(self.entry.pk), "section": "2 Bernoulli"}, client
        )
        card = payload["study"]["data"]
        self.assertTrue(card["has_more"])
        self.assertEqual(card["next"], {"section": "2 Bernoulli", "part": 2})
        self.assertEqual(card["page_range"], [3, 3])


class OverviewTests(Base):
    def test_counts_only_what_the_reader_sees(self):
        context = self.context(user="anonymous")
        with mock.patch("apps.graph.queries.top_concepts", return_value=[]):
            payload, text = tools.run(context.request, tools.LIBRARY_OVERVIEW, {}, context=context)
        self.assertEqual(payload["publications"], 3)  # not the hidden catalog's
        self.assertEqual(payload["processed"], 3)
        self.assertIn("publications", json.loads(text))


class PackTests(Base):
    def test_needs_pack(self):
        for text in ("Ahoj!", "ďakujem", "ok", "Čo všetko vieš?", "Showcase what you can do"):
            self.assertFalse(pack.needs_pack(text), text)
        self.assertTrue(pack.needs_pack("Ako funguje Bernoulliho rovnica?"))

    def _search(self, *passages, degraded=False):
        hit = SimpleNamespace(entry_id=self.entry.pk, passages=list(passages))
        return mock.patch(
            "apps.extraction.discovery.search_passages",
            return_value=SimpleNamespace(degraded=degraded, items=[hit], indexed=1),
        )

    def _passage(self, paragraph, score, matched=("semantic",), text="text"):
        return SimpleNamespace(
            paragraph_id=paragraph.pk,
            page=paragraph.page,
            section=paragraph.section,
            score=score,
            text=text,
            matched=list(matched),
        )

    def test_the_pack_keeps_relevant_passages_and_the_figures_they_refer_to(self):
        context = self.context()
        with (
            self._search(self._passage(self.paragraphs[3], 0.9), self._passage(self.paragraphs[0], 0.5)),
            mock.patch("apps.extraction.regions.search_regions", return_value=[]),
            mock.patch.object(pack, "_publications", return_value=[{"id": "x", "title": "T"}]),
        ):
            payload = pack.gather(context, "Ako funguje Venturiho trubica?")
        self.assertEqual([p["label"] for p in payload["passages"]], ["E1"])
        self.assertEqual([f["label"] for f in payload["figures"]], ["F1"])
        seen = pack.slim(payload)
        self.assertNotIn("evidence", seen)
        self.assertNotIn("paragraph_id", seen["passages"][0])
        self.assertNotIn("score", seen["passages"][0])
        sources = pack.sources(payload, context.request)
        self.assertEqual([s["label"] for s in sources["sources"]], ["E1", "F1"])
        self.assertIn("image_url", sources["sources"][1])

    def test_an_empty_pack_says_so(self):
        context = self.context()
        with (
            self._search(self._passage(self.paragraphs[0], 0.4)),
            mock.patch("apps.extraction.regions.search_regions", return_value=[]),
            mock.patch.object(pack, "_publications", return_value=[]),
        ):
            payload = pack.gather(context, "alpaky v Peru")
        self.assertTrue(payload["low_relevance"])
        self.assertIn("No new evidence", payload["note"])
        self.assertIn("earlier results", payload["note"])
