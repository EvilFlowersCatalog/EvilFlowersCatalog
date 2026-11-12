"""The readable tool-call log sent with `status` and `activity` frames (IP-022 §4, portal #36)."""

import json

from django.test import SimpleTestCase

from apps.assistant import activity


class DescribeTests(SimpleTestCase):
    def test_query_is_echoed_and_trimmed(self):
        detail = activity.describe("search_entries", {"query": "  machine\n learning "})
        self.assertEqual(detail, {"query": "machine learning"})

    def test_read_pages_become_a_page_list(self):
        self.assertEqual(activity.describe("read_pdf_page", {"page": 3, "count": 3}), {"pages": [3, 4, 5]})

    def test_highlight_request_reads_as_query(self):
        self.assertEqual(activity.describe("create_highlights", {"prompt": "entropy"}), {"query": "entropy"})

    def test_ids_are_not_echoed(self):
        self.assertEqual(activity.describe("get_entry", {"entry_id": "0b7e…"}), {})


class SummarizeTests(SimpleTestCase):
    def test_catalog_search_lists_titles_and_total(self):
        payload = {
            "items": [{"id": "a", "title": "Deep Learning"}, {"id": "b", "title": "Pattern Recognition"}],
            "metadata": {"total": 12},
        }
        summary = activity.summarize("search_entries", {"query": "ml"}, payload, "{}")
        self.assertEqual(summary["count"], 12)
        self.assertEqual(summary["query"], "ml")
        self.assertEqual(
            summary["entries"], [{"id": "a", "title": "Deep Learning"}, {"id": "b", "title": "Pattern Recognition"}]
        )

    def test_entry_without_title_is_left_out(self):
        summary = activity.summarize("search_entries", {}, {"items": [{"id": "a"}]}, "{}")
        self.assertNotIn("entries", summary)

    def test_get_entry_is_its_title_without_a_count(self):
        summary = activity.summarize("get_entry", {"entry_id": "a"}, {"entry": {"id": "a", "title": "SICP"}}, "{}")
        self.assertEqual(summary["entries"], [{"id": "a", "title": "SICP"}])
        self.assertNotIn("count", summary)

    def test_loans_use_entry_title(self):
        payload = {"items": [{"entry_id": "a", "entry_title": "Dune"}]}
        summary = activity.summarize("list_my_loans", {}, payload, "{}")
        self.assertEqual(summary["entries"], [{"id": "a", "title": "Dune"}])

    def test_read_pages_reports_pages_read(self):
        text = json.dumps({"pages": [{"page": 4, "text": "…"}, {"page": 5, "text": "…"}]})
        summary = activity.summarize("read_pdf_page", {"page": 4, "count": 3}, text=text)
        self.assertEqual(summary["pages"], [4, 5])
        self.assertNotIn("count", summary)

    def test_pdf_search_reports_a_count(self):
        text = json.dumps({"results": [{"page": 9, "score": 2}, {"page": 2, "score": 1}]})
        summary = activity.summarize("search_pdf", {"query": "entropy"}, text=text)
        self.assertEqual(summary, {"tool": "search_pdf", "query": "entropy", "count": 2})

    def test_error_result_is_flagged(self):
        summary = activity.summarize("read_pdf_page", {"page": 999}, text=json.dumps({"error": "No such page."}))
        self.assertTrue(summary["error"])
        self.assertNotIn("count", summary)


class FromHistoryTests(SimpleTestCase):
    """The log rebuilt from stored rows when a chat is read back (it was lost on refresh)."""

    @staticmethod
    def _row(pk, role, text="", tool_calls=None, tool_result=None):
        from types import SimpleNamespace

        visible = role == "user" or (role == "assistant" and tool_calls is None and bool(text))
        return SimpleNamespace(
            pk=pk, role=role, text=text, tool_calls=tool_calls, tool_result=tool_result, is_visible=visible
        )

    def test_answer_carries_its_turns_calls(self):
        from apps.assistant.turn import REPEATED_CALL

        def call(name, **arguments):
            return {"function": {"name": name, "arguments": arguments}}

        rows = [
            self._row(1, "user", "books on ml?"),
            self._row(
                2, "assistant", tool_calls=[call("search_entries", query="ml"), call("search_entries", query="ml")]
            ),
            self._row(3, "tool", "{}", tool_result={"items": [{"id": "a", "title": "Deep Learning"}]}),
            self._row(4, "tool", REPEATED_CALL),
            self._row(5, "assistant", tool_calls=[call("displayBooks", entry_ids=["a"])]),
            self._row(6, "tool"),
            self._row(7, "assistant", "Here is one."),
            self._row(8, "user", "thanks"),
            self._row(9, "assistant", "You're welcome."),
        ]
        visible = activity.from_history(rows)

        self.assertEqual([row.pk for row in visible], [1, 7, 8, 9])
        self.assertEqual(
            visible[1].activity,
            [
                {
                    "tool": "search_entries",
                    "query": "ml",
                    "count": 1,
                    "entries": [{"id": "a", "title": "Deep Learning"}],
                    "id": "3",
                }
            ],
        )
        self.assertIsNone(visible[3].activity)
