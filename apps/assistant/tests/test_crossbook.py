"""#157: evidence from other books over SIMILAR_TO, compare mode, and passages in another language.

The graph tests need PostgreSQL with Apache AGE and skip elsewhere (see `apps.graph.tests.test_graph`).
"""

import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant import crossbook, evidence, pack, tools
from apps.assistant.library import ToolContext, label_units
from apps.assistant.tests import fixtures
from apps.core.models import Language
from apps.extraction.models import ParagraphLink, SectionSimilarity
from apps.graph.tests.test_graph import GraphFixture


def _anonymous_context(language="sk", **kwargs) -> ToolContext:
    request = fixtures.request_for(None)
    request.user = AnonymousUser()
    return ToolContext(request=request, ledger=evidence.Ledger(), reading=tools.Reading(), language=language, **kwargs)


class MarkerTests(SimpleTestCase):
    def test_marker_is_in_the_readers_language(self):
        self.assertEqual(crossbook.marker("sk", "en"), "(anglicky)")
        self.assertEqual(crossbook.marker("sk", "de"), "(nemecky)")
        self.assertEqual(crossbook.marker("en", "de"), "(in German)")
        self.assertEqual(crossbook.marker("en-GB", "sk"), "(in Slovak)")

    def test_no_marker_for_the_same_or_an_unknown_language(self):
        self.assertIsNone(crossbook.marker("sk", "sk"))
        self.assertIsNone(crossbook.marker("en", None))
        # An unknown reader language is treated as Slovak, the library's default.
        self.assertEqual(crossbook.marker(None, "en"), "(anglicky)")
        self.assertEqual(crossbook.marker("sk", "fi"), "(fi)")


class LanguageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.rows, _v, _s = fixtures.publication(cls.reader, cls.open)
        cls.other, cls.other_run, cls.other_rows, _f, _s2 = fixtures.publication(
            cls.reader, cls.open, title="Hydraulics"
        )
        english, _ = Language.objects.get_or_create(alpha2="en", defaults={"name": "English"})
        slovak, _ = Language.objects.get_or_create(alpha2="sk", defaults={"name": "Slovak"})
        cls.entry.language = slovak
        cls.entry.save()
        cls.other.language = english
        cls.other.save()
        ParagraphLink.objects.create(
            source=cls.rows[2], target=cls.other_rows[2], kind=ParagraphLink.Kind.SIMILAR, score=0.9
        )

    def test_a_passage_in_another_language_is_marked_and_cited_with_its_language(self):
        context = _anonymous_context()
        units = [
            {"kind": "passage", "entry_id": str(self.entry.pk), "page": 3, "paragraph_id": str(self.rows[2].pk)},
            {"kind": "passage", "entry_id": str(self.other.pk), "page": 3, "paragraph_id": str(self.other_rows[2].pk)},
        ]
        note = crossbook.mark_languages(context, units)
        self.assertNotIn("marker", units[0])
        self.assertEqual((units[1]["language"], units[1]["marker"]), ("en", "(anglicky)"))
        self.assertIn("Slovak", note)
        self.assertIn("(anglicky)", note)
        self.assertIn("Never present your translation as a quotation", note)
        issued = label_units(context.ledger, units)
        self.assertNotIn("language", issued["E1"])
        self.assertEqual(issued["E2"]["language"], "en")
        citations, _unknown = context.ledger.cite("Tlak klesá [E2].")
        self.assertEqual(citations[0]["language"], "en")
        self.assertEqual(citations[0]["entry_id"], str(self.other.pk))

    def test_followed_passages_carry_the_marker_and_the_note(self):
        context = _anonymous_context()
        label, _ = context.ledger.issue(
            {"kind": "passage", "entry_id": str(self.entry.pk), "page": 3, "paragraph_id": str(self.rows[2].pk)}
        )
        _payload, text = tools.run(
            context.request,
            tools.FOLLOW,
            {"label": label, "relation": "similar_elsewhere"},
            ledger=context.ledger,
            context=context,
        )
        seen = json.loads(text)
        self.assertEqual(seen["passages"][0]["marker"], "(anglicky)")
        self.assertIn("language_note", seen)

    def test_same_language_adds_nothing(self):
        context = _anonymous_context(language="en")
        units = [{"kind": "passage", "entry_id": str(self.other.pk), "page": 3}]
        self.assertIsNone(crossbook.mark_languages(context, units))
        self.assertNotIn("marker", units[0])


class CrossBookGraphTests(GraphFixture):
    """Hydraulics' "2 Bernoulli" is SIMILAR_TO Fluid Mechanics'; so is Secret's, closer, in a hidden catalog."""

    def setUp(self):
        super().setUp()
        SectionSimilarity.objects.create(
            source=self.run,
            source_section="2 Bernoulli",
            target=self.secret_run,
            target_section="2 Bernoulli",
            score=0.97,
        )
        self._project_all()

    def _pack(self, context, *paragraphs):
        hit = SimpleNamespace(
            entry_id=self.entry.pk,
            passages=[
                SimpleNamespace(
                    paragraph_id=row.pk,
                    page=row.page,
                    section=row.section,
                    score=0.9,
                    text=row.text,
                    matched=["semantic"],
                )
                for row in paragraphs
            ],
        )
        with (
            mock.patch(
                "apps.extraction.discovery.search_passages",
                return_value=SimpleNamespace(degraded=False, items=[hit], indexed=1),
            ),
            mock.patch("apps.extraction.regions.search_regions", return_value=[]),
        ):
            return pack.gather(context, "Ako súvisí tlak a rýchlosť?", entry=self.entry)

    def test_a_question_inside_one_book_gets_sections_of_other_books(self):
        context = _anonymous_context()
        payload = self._pack(context, self.rows[3])
        elsewhere = payload[crossbook.FROM_OTHER_BOOKS]
        # Secret is the closest, but hidden from this reader: never evidence.
        self.assertEqual({passage["entry_id"] for passage in elsewhere}, {str(self.other.pk)})
        self.assertEqual(elsewhere[0]["section"], "2 Bernoulli")
        self.assertEqual(elsewhere[0]["page"], 3)
        self.assertEqual([p["label"] for p in payload["passages"]], ["E1"])
        self.assertEqual(elsewhere[0]["label"], "E2")
        self.assertEqual(context.ledger.sources["E2"]["entry_id"], str(self.other.pk))
        self.assertEqual(context.ledger.sources["E2"]["section"], "2 Bernoulli")
        seen = pack.slim(payload)
        self.assertIn(crossbook.FROM_OTHER_BOOKS, seen)
        self.assertNotIn("via", seen[crossbook.FROM_OTHER_BOOKS][0])
        sources = pack.sources(payload, context.request)
        # The figure the cited passage refers to (Obr. 2.1) comes after the evidence from other books.
        self.assertEqual([source["label"] for source in sources["sources"]], ["E1", "E2", "F1"])
        # Rebuilt from the stored tool row, the ledger keeps the other book's text for the support check.
        self.assertIn("E2", evidence._texts(payload))

    def test_the_hidden_book_appears_for_a_reader_who_may_see_it(self):
        context = ToolContext(
            request=fixtures.request_for(self.reader), ledger=evidence.Ledger(), reading=tools.Reading()
        )
        with mock.patch(
            "apps.graph.queries.visible_entry_ids",
            return_value=[str(self.entry.pk), str(self.other.pk), str(self.secret.pk)],
        ):
            found = crossbook.similar_sections(context, [(str(self.run.pk), "2 Bernoulli")], exclude=[])
        self.assertEqual(found[0]["entry_id"], str(self.secret.pk))

    @override_settings(EVILFLOWERS_ASSISTANT_PACK_ELSEWHERE=0)
    def test_widening_can_be_switched_off(self):
        payload = self._pack(_anonymous_context(), self.rows[3])
        self.assertNotIn(crossbook.FROM_OTHER_BOOKS, payload)

    def test_without_the_graph_nothing_is_added(self):
        with mock.patch("apps.graph.projection.available", return_value=False):
            payload = self._pack(_anonymous_context(), self.rows[3])
        self.assertNotIn(crossbook.FROM_OTHER_BOOKS, payload)
        self.assertEqual([p["label"] for p in payload["passages"]], ["E1"])

    def _compare(self, context, label, **arguments):
        payload, text = tools.run(
            context.request,
            tools.FOLLOW,
            {"label": label, "relation": "compare", **arguments},
            ledger=context.ledger,
            context=context,
        )
        return payload, json.loads(text)

    def _label(self, context, row, entry=None):
        label, _ = context.ledger.issue(
            {
                "kind": "passage",
                "entry_id": str((entry or self.entry).pk),
                "page": row.page,
                "paragraph_id": str(row.pk),
            }
        )
        return label

    def test_compare_follows_similar_to_into_the_other_book(self):
        context = _anonymous_context()
        label = self._label(context, self.rows[3])
        payload, seen = self._compare(context, label, entry_id=str(self.other.pk))
        self.assertEqual(payload["passages"][0]["paragraph_id"], str(self.rows[3].pk))
        self.assertEqual(payload["passages"][0]["label"], label)
        self.assertEqual({p["entry_id"] for p in payload["passages"][1:]}, {str(self.other.pk)})
        self.assertEqual(payload["compare"]["a"]["labels"], [label])
        self.assertEqual(payload["compare"]["b"]["entry_id"], str(self.other.pk))
        self.assertTrue(payload["compare"]["b"]["labels"])
        self.assertIn("side by side", seen["note"])
        self.assertNotIn("paragraph_id", seen["passages"][1])

    def test_compare_without_a_book_picks_the_most_similar_visible_one(self):
        context = _anonymous_context()
        payload, _seen = self._compare(context, self._label(context, self.rows[3]))
        self.assertEqual(payload["compare"]["b"]["entry_id"], str(self.other.pk))

    def test_compare_with_a_hidden_book_is_refused(self):
        context = _anonymous_context()
        payload, seen = self._compare(context, self._label(context, self.rows[3]), entry_id=str(self.secret.pk))
        self.assertIsNone(payload)
        self.assertIn("error", seen)
        self.assertNotIn("Secret", json.dumps(seen))

    def test_compare_with_the_same_book_is_explained(self):
        context = _anonymous_context()
        _payload, seen = self._compare(context, self._label(context, self.rows[3]), entry_id=str(self.entry.pk))
        self.assertIn("another publication", seen["error"])

    def test_compare_without_the_graph_says_so(self):
        context = _anonymous_context()
        label = self._label(context, self.rows[3])
        with mock.patch("apps.graph.projection.available", return_value=False):
            payload, seen = self._compare(context, label, entry_id=str(self.other.pk))
        self.assertEqual([p["label"] for p in payload["passages"]], [label])
        self.assertIn("Hydraulics", seen["note"])
