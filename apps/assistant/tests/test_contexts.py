from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.assistant import contexts, services
from apps.assistant.models import ChatMessage

BOX = {"x0": 0.1, "y0": 0.2, "x1": 0.6, "y1": 0.4}


class NormalizeTests(SimpleTestCase):
    def test_empty_is_none(self):
        for value in (None, "", []):
            self.assertIsNone(contexts.normalize(value))

    def test_selection_and_explain(self):
        stored = contexts.normalize(
            [
                {"kind": "selection", "text": "  relational model ", "page": "12", "extra": "dropped"},
                {"kind": "explain", "text": "this method", "page": 3, "explanation": "It is X.", "regionId": "r1"},
            ]
        )
        self.assertEqual(stored[0], {"kind": "selection", "text": "relational model", "page": 12})
        self.assertEqual(
            stored[1],
            {"kind": "explain", "text": "this method", "page": 3, "explanation": "It is X.", "regionId": "r1"},
        )

    def test_region_drops_session_url_and_clamps_box(self):
        [stored] = contexts.normalize(
            [
                {
                    "kind": "region",
                    "region": {
                        "id": "r1",
                        "kind": "table",
                        "page": 4,
                        "bbox": {"x0": -1, "y0": 0.2, "x1": 2, "y1": 0.4},
                        "markdown": "| a |\n|---|\n| 1 |",
                        "imageUrl": "https://catalog/crops/r1?token=secret",
                    },
                }
            ]
        )
        self.assertNotIn("imageUrl", stored["region"])
        self.assertEqual(stored["region"]["bbox"], {"x0": 0.0, "y0": 0.2, "x1": 1.0, "y1": 0.4})
        self.assertEqual(contexts.page_of(stored), 4)

    def test_highlight(self):
        [stored] = contexts.normalize(
            [
                {
                    "kind": "highlight",
                    "annotationId": "a1",
                    "highlight": {"id": "h1", "seq": 2, "page": 7, "quads": [BOX], "text": "A sentence."},
                }
            ]
        )
        self.assertEqual(stored["highlight"]["quads"], [BOX])
        self.assertIsNone(stored["highlight"]["note"])

    def test_rejects_unknown_kind_bad_page_and_too_many(self):
        with self.assertRaises(contexts.InvalidContext):
            contexts.normalize([{"kind": "image", "page": 1}])
        with self.assertRaises(contexts.InvalidContext):
            contexts.normalize([{"kind": "selection", "text": "x", "page": 0}])
        with self.assertRaises(contexts.InvalidContext):
            contexts.normalize({"kind": "selection"})
        with self.assertRaises(contexts.InvalidContext):
            contexts.normalize([{"kind": "selection", "text": "x", "page": 1}] * (contexts.MAX_CONTEXTS + 1))


class ReplayTests(SimpleTestCase):
    def _row(self, **kwargs):
        defaults = {
            "role": ChatMessage.Role.USER,
            "text": "What does it mean?",
            "entry": None,
            "page": None,
            "contexts": None,
        }
        defaults.update(kwargs)
        return SimpleNamespace(tool_calls=None, **defaults)

    def test_plain_message_is_unchanged(self):
        self.assertEqual(services._as_message(self._row())["content"], "What does it mean?")

    def test_page_and_contexts_are_noted(self):
        row = self._row(
            page=5,
            contexts=contexts.normalize([{"kind": "selection", "text": "the theorem above", "page": 5}]),
        )
        content = services._as_message(row)["content"]
        self.assertIn("The reader is on page 5 of the viewer.", content)
        self.assertIn('Selected text on page 5: """the theorem above"""', content)
        self.assertTrue(content.endswith("What does it mean?"))

    def test_reading_note_mentions_citations(self):
        note = services.reading_note(SimpleNamespace(page_count=42))
        self.assertIn("42 pages", note)
        self.assertIn("[[page:N]]", note)
