"""#179: `annotate_figure` — marks on a cited figure, grounded by the vision model."""

import io
import json
from contextlib import contextmanager
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant import citations, evidence, marks, services, tools
from apps.assistant.library import ToolContext
from apps.assistant.tests import fixtures
from apps.assistant.tests.test_ip030_tools import JsonClient
from apps.mcp.errors import ToolError

ON = {"EVILFLOWERS_ASSISTANT_FIGURE_MARKS": True, "EVILFLOWERS_ASSISTANT_VISION": True}


class SanitiseTests(SimpleTestCase):
    def test_scale_1000_becomes_fractions_with_ordered_corners(self):
        self.assertEqual(
            marks.sanitise({"shape": "box", "x0": 700, "y0": 300, "x1": 300, "y1": 20}),
            {"shape": "box", "x0": 0.3, "y0": 0.02, "x1": 0.7, "y1": 0.3},
        )

    def test_fractions_are_kept_and_everything_is_clamped(self):
        self.assertEqual(
            marks.sanitise({"shape": "point", "x0": 0.21, "y0": 0.88}), {"shape": "point", "x0": 0.21, "y0": 0.88}
        )
        self.assertEqual(
            marks.sanitise({"shape": "arrow", "x0": -50, "y0": 750, "x1": 1300, "y1": 300}),
            {"shape": "arrow", "x0": 0.0, "y0": 0.75, "x1": 1.0, "y1": 0.3},
        )

    def test_qwen_bbox_2d_is_a_box(self):
        self.assertEqual(
            marks.sanitise({"bbox_2d": [100, 200, 500, 600]}),
            {"shape": "box", "x0": 0.1, "y0": 0.2, "x1": 0.5, "y1": 0.6},
        )

    def test_unusable_marks_are_dropped(self):
        for item in (
            {"shape": "star", "x0": 1, "y0": 1, "x1": 2, "y1": 2},
            {"shape": "box", "x0": 100, "y0": 100, "x1": 104, "y1": 400},  # thinner than MIN_SIDE
            {"shape": "arrow", "x0": 10, "y0": 10, "x1": 10, "y1": 10},
            {"shape": "box", "x0": 10, "y0": "a", "x1": 20, "y1": 20},
            {"shape": "point", "x0": float("nan"), "y0": 1},
        ):
            self.assertIsNone(marks.sanitise(item), item)

    def test_parse_reads_fenced_json_by_id_first_wins(self):
        text = (
            "```json\n"
            + json.dumps(
                {
                    "marks": [
                        {"id": 2, "shape": "point", "x0": 500, "y0": 500},
                        {"id": 2, "shape": "point", "x0": 10, "y0": 10},
                        {"id": 7, "shape": "point", "x0": 10, "y0": 10},
                        {"id": "x"},
                    ]
                }
            )
            + "\n```"
        )
        self.assertEqual(marks.parse(text, 3), {1: {"shape": "point", "x0": 0.5, "y0": 0.5}})
        self.assertEqual(marks.parse("I cannot see it.", 3), {})


class OfferTests(SimpleTestCase):
    def names(self, **kwargs):
        return [spec["function"]["name"] for spec in tools.specifications(**kwargs)]

    def test_off_by_default_and_needs_vision(self):
        self.assertNotIn(tools.ANNOTATE_FIGURE, self.names())
        with override_settings(EVILFLOWERS_ASSISTANT_FIGURE_MARKS=True, EVILFLOWERS_ASSISTANT_VISION=False):
            self.assertNotIn(tools.ANNOTATE_FIGURE, self.names())
        with override_settings(**ON):
            self.assertIn(tools.ANNOTATE_FIGURE, self.names())
            self.assertIn(tools.ANNOTATE_FIGURE, self.names(authenticated=True, bound=True))

    @override_settings(**ON)
    def test_first_round_prefix_with_marks_on(self):
        # 2026-11-15: ~2,600 estimated tokens signed in with the tool, past the 2,450 CPU budget held while it is
        # off (`test_tool_surface`). Pinned so the switch's cost is a known number, not drift. #185/#186 added ~100
        # to both: 2,700 with the tool, against the 2,550 budget without it.
        specs = json.dumps(tools.specifications(authenticated=True), ensure_ascii=False)
        estimate = (len(specs) + len(services.SYSTEM_PROMPT)) / 3.4
        self.assertLessEqual(estimate, 2720)


@contextmanager
def crop():
    storage = mock.Mock()
    storage.open.side_effect = lambda *args, **kwargs: io.BytesIO(b"webp")
    with mock.patch("apps.files.storage.get_storage", return_value=storage):
        yield storage


@override_settings(**ON)
class AnnotateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, _run, _rows, cls.venturi, cls.shear = fixtures.publication(cls.reader, cls.open)
        cls.licensed, _r, _p, cls.licensed_figure, _s = fixtures.publication(
            cls.reader, cls.open, title="Licensed", readium=True
        )

    def context(self):
        return ToolContext(request=fixtures.request_for(self.reader), ledger=evidence.Ledger(), language="en")

    def issue(self, context, region, entry=None):
        label, _ = context.ledger.issue(
            {"kind": "figure", "entry_id": str((entry or self.entry).pk), "page": 4, "region_id": str(region.pk)}
        )
        return label

    TARGETS = [
        {"hint": "the narrow throat", "label": "Throat", "note": "The speed is highest here."},
        {"hint": "the pressure gauge on the inlet", "label": "Inlet gauge"},
    ]

    def annotate(self, context, label, answer, targets=None):
        client = JsonClient(answer)
        with crop(), mock.patch("apps.assistant.llm.client", return_value=client):
            payload = tools.call(
                context.request,
                tools.ANNOTATE_FIGURE,
                {"label": label, "targets": targets or self.TARGETS},
                context=context,
            )
        return payload, client

    def test_marks_ride_on_every_later_citation_of_the_figure(self):
        context = self.context()
        label = self.issue(context, self.venturi)
        payload, client = self.annotate(
            context, label, {"marks": [{"id": 1, "shape": "ellipse", "x0": 400, "y0": 300, "x1": 600, "y1": 700}]}
        )

        self.assertEqual(payload["not_found"], ["the pressure gauge on the inlet"])
        expected = [
            {
                "shape": "ellipse",
                "x0": 0.4,
                "y0": 0.3,
                "x1": 0.6,
                "y1": 0.7,
                "label": "Throat",
                "note": "The speed is highest here.",
            }
        ]
        self.assertEqual(payload["marks"], expected)
        # The crop and the hints went to the grounding request.
        request = client.requests[0]["messages"][1]
        self.assertEqual(request["images"], ["d2VicA=="])
        self.assertIn("1. the narrow throat", request["content"])
        # The answer's citation carries them, and so does the frame for a label already cited.
        found, _unknown = context.ledger.cite(f"The throat is narrower [{label}].", context.request)
        self.assertEqual(found[0]["marks"], expected)
        self.assertIn("image_url", found[0])
        ((event, frame),) = context.frames
        self.assertEqual((event, frame["label"], frame["marks"]), ("citation", label, expected))
        # The model sees what was drawn, not coordinates or ids.
        self.assertEqual(
            tools.slim(tools.ANNOTATE_FIGURE, payload),
            {
                "label": label,
                "drawn": ["Throat"],
                "not_found": ["the pressure gauge on the inlet"],
                "note": f"Cite [{label}] where you explain it; the marks are shown on it.",
            },
        )

    def test_nothing_located_changes_nothing(self):
        context = self.context()
        label = self.issue(context, self.venturi)
        payload, _client = self.annotate(context, label, "Sorry, I cannot tell.")
        self.assertEqual(payload["marks"], [])
        self.assertNotIn("marks", context.ledger.sources[label])
        self.assertEqual(context.frames, [])

    def test_targets_are_capped_and_clipped(self):
        context = self.context()
        label = self.issue(context, self.venturi)
        targets = [{"hint": f"part {n}", "label": "L" * 100, "note": "N" * 400} for n in range(9)]
        answer = {"marks": [{"id": n, "shape": "point", "x0": 100 * n, "y0": 100} for n in range(1, 10)]}
        payload, _client = self.annotate(context, label, answer, targets)
        self.assertEqual(len(payload["marks"]), marks.MAX_TARGETS)
        self.assertEqual(len(payload["marks"][0]["label"]), marks.LABEL_CHARS)
        self.assertEqual(len(payload["marks"][0]["note"]), marks.NOTE_CHARS)

    def refused(self, context, label, message):
        with self.assertRaises(ToolError) as raised:
            self.annotate(context, label, {"marks": []})
        self.assertIn(message, str(raised.exception))

    def test_only_figures_of_this_turn(self):
        context = self.context()
        passage, _ = context.ledger.issue({"kind": "passage", "entry_id": str(self.entry.pk), "paragraph_id": "p1"})
        self.refused(context, passage, "F# figure label")
        self.refused(context, "F9", "F# figure label")
        # An earlier turn's figure, once this turn has evidence of its own (#162).
        earlier = evidence.Ledger(
            {"F1": {"kind": "figure", "entry_id": str(self.entry.pk), "region_id": str(self.venturi.pk)}}
        )
        context = ToolContext(request=context.request, ledger=earlier)
        context.ledger.issue({"kind": "passage", "entry_id": str(self.entry.pk), "paragraph_id": "p2"})
        self.refused(context, "F1", "F# figure label")

    def test_never_for_a_figure_the_reader_cannot_see_or_without_a_crop(self):
        context = self.context()
        self.refused(context, self.issue(context, self.licensed_figure, self.licensed), "cannot see")
        self.refused(context, self.issue(context, self.shear), "no image")

    @override_settings(EVILFLOWERS_ASSISTANT_VISION=False)
    def test_refused_without_vision(self):
        context = self.context()
        self.refused(context, self.issue(context, self.venturi), "explain the figure in words")


@override_settings(**ON)
class TurnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, _run, _rows, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)

    def test_the_answer_citing_the_figure_carries_the_marks_and_nothing_is_sent_before_it(self):
        from apps.assistant import turn
        from apps.assistant.evaluation import parse_frames
        from apps.assistant.models import Chat, ChatMessage
        from apps.assistant.tests.test_turn_tools import ScriptedClient, _call

        chat = Chat.objects.create(user=self.reader)
        figure = {"kind": "figure", "entry_id": str(self.entry.pk), "page": 4, "region_id": str(self.venturi.pk)}
        ChatMessage.objects.create(
            chat=chat,
            user=self.reader,
            role=ChatMessage.Role.TOOL,
            text="{}",
            tool_result={"evidence": {"F1": figure}},
        )
        target = {"hint": "the narrow throat", "label": "Throat"}
        client = ScriptedClient([[_call(tools.ANNOTATE_FIGURE, label="F1", targets=[target])], "Zúženie [F1]."])
        grounding = JsonClient({"marks": [{"id": 1, "shape": "box", "x0": 400, "y0": 300, "x1": 600, "y1": 700}]})
        with (
            crop(),
            mock.patch.object(turn, "make_client", return_value=client),
            mock.patch("apps.assistant.llm.client", return_value=grounding),
        ):
            frames = list(turn.run(fixtures.request_for(self.reader), chat, "Ukáž mi zúženie.", language="sk"))
        events = parse_frames(frames)

        expected = [{"shape": "box", "x0": 0.4, "y0": 0.3, "x1": 0.6, "y1": 0.7, "label": "Throat"}]
        streamed = [data for event, data in events if event == "citation"]
        self.assertEqual([(data["label"], data["marks"]) for data in streamed], [("F1", expected)])
        answer = chat.messages.visible().filter(role=ChatMessage.Role.ASSISTANT).get()
        self.assertEqual(answer.citations[0]["marks"], expected)
        # The next turn's ledger starts without them.
        self.assertNotIn("marks", evidence.Ledger.for_chat(chat).sources["F1"])


class CitationTests(SimpleTestCase):
    def test_marks_are_part_of_a_citation(self):
        source = {"kind": "figure", "region_id": "r", "marks": [{"shape": "point", "x0": 0.1, "y0": 0.2}]}
        self.assertEqual(citations.citation("F1", source)["marks"], source["marks"])
