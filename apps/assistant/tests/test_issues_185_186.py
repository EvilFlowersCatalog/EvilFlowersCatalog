"""#185 (figures in answers) and #186 (the content layer in the chat)."""

import json
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant import activity, citations, evidence, follow, tools, turn, withheld
from apps.assistant.library import ToolContext
from apps.assistant.models import ChatMessage
from apps.assistant.tests import fixtures
from apps.assistant.tests.test_issues_162_168 import TurnBase
from apps.assistant.tests.test_turn_tools import ScriptedClient, _call
from apps.extraction.models import Region


def _pack_with_figure(entry, paragraph, region):
    payload = {
        "query": "Venturi",
        "publications": [],
        "passages": [
            {
                "kind": "passage",
                "entry_id": str(entry.pk),
                "title": entry.title,
                "page": 4,
                "paragraph_id": str(paragraph.pk),
                "text": "The Venturi tube in Obr. 2.1 shows the effect.",
                "label": "E1",
            }
        ],
        "figures": [{"kind": "figure", "entry_id": str(entry.pk), "page": 4, "region_id": str(region.pk)}],
        "evidence": {
            "E1": {"kind": "passage", "entry_id": str(entry.pk), "page": 4, "paragraph_id": str(paragraph.pk)},
            "F1": {"kind": "figure", "entry_id": str(entry.pk), "page": 4, "region_id": str(region.pk)},
        },
    }

    def gather(context, query, entry=None):
        context.ledger.issue(payload["evidence"]["E1"])
        context.ledger.issue(payload["evidence"]["F1"])
        return payload

    return gather


class ShowFiguresTests(TurnBase):
    """#185 §1: the gallery directive."""

    @override_settings(EVILFLOWERS_ASSISTANT_PREFETCH=True)
    def test_a_gallery_closes_the_turn_with_crops_first(self):
        client = ScriptedClient([[_call(tools.SHOW_FIGURES, labels=["f1", "F9", "E1"])]])
        gather = _pack_with_figure(self.entry, self.paragraphs[3], self.venturi)
        with mock.patch("apps.assistant.pack.gather", side_effect=gather):
            chat, events = self._turn(client, text="Ukáž mi obrázok Venturiho trubice.")

        names = [event for event, _data in events]
        self.assertIn("figures", names)
        # The crop arrives before the gallery that shows it.
        first_citation = next(
            i for i, (event, data) in enumerate(events) if event == "citation" and data["label"] == "F1"
        )
        self.assertLess(first_citation, names.index("figures"))
        self.assertEqual(dict(events)["figures"], {"labels": ["F1"]})
        self.assertEqual(len(client.requests), 1)  # the directive is the answer

        refused = json.loads(chat.messages.filter(role=ChatMessage.Role.TOOL).order_by("created_at").last().text)
        self.assertEqual(refused["shown"], ["F1"])
        self.assertIn("F9", refused["error"])
        self.assertIn("E1", refused["error"])

        answer = chat.messages.visible().filter(role=ChatMessage.Role.ASSISTANT).get()
        self.assertEqual(answer.displayed_figures, ["F1"])
        self.assertEqual([item["label"] for item in answer.citations], ["F1"])
        self.assertEqual(answer.citations[0]["printed_label"], "Obr. 2.1")

        shown = [data for event, data in events if event == "activity" and data["tool"] == tools.SHOW_FIGURES]
        self.assertEqual(shown[0]["count"], 1)
        self.assertEqual(shown[0]["labels"], ["F1"])


class ShowableTests(SimpleTestCase):
    def test_figures_of_this_turn_only_each_once_capped(self):
        ledger = evidence.Ledger({"F1": {"region_id": "r1"}, "E1": {"paragraph_id": "p1"}})
        for number in range(2, 12):
            ledger.issue({"region_id": f"r{number}"})
        labels = [f"F{number}" for number in range(2, 12)]
        accepted, rejected = turn._showable(ledger, ["F2", "f2", "E1", "F1", *labels])
        # F1 is from an earlier turn while this one has evidence of its own; E1 is no figure.
        self.assertEqual(accepted, labels[: tools.MAX_SHOWN_FIGURES])
        self.assertIn("F1", rejected)
        self.assertIn("E1", rejected)
        self.assertIn("F11", rejected)
        self.assertEqual(turn._showable(ledger, "F3, F4")[0], ["F3", "F4"])
        self.assertEqual(turn._showable(None, ["F1"]), ([], ["F1"]))

    def test_activity_counts_shown_figures_despite_a_partial_refusal(self):
        text = json.dumps({"shown": ["F2"], "error": "Not shown: F9."})
        summary = activity.summarize(tools.SHOW_FIGURES, {"labels": ["F2", "F9"]}, text=text)
        self.assertEqual((summary["count"], summary["labels"]), (1, ["F2"]))
        self.assertNotIn("error", summary)
        self.assertTrue(
            activity.summarize(tools.SHOW_FIGURES, {}, text=json.dumps({"shown": [], "error": "x"}))["error"]
        )


class FiguresOnlyTests(SimpleTestCase):
    """#185 §3: figure-only retrieval without a new tool."""

    def test_modes(self):
        self.assertEqual(tools.figures_mode(True), tools.FIGURES_ALSO)
        self.assertEqual(tools.figures_mode("also"), tools.FIGURES_ALSO)
        self.assertEqual(tools.figures_mode("ONLY"), tools.FIGURES_ONLY)
        self.assertIsNone(tools.figures_mode(None))
        self.assertIsNone(tools.figures_mode(False))

    def test_only_runs_the_figure_search(self):
        hits = [{"kind": "table", "entry_id": "e", "page": 3, "region_id": "r", "caption": "Table 8.1"}]
        with (
            mock.patch.object(tools, "_figures", return_value=hits) as figures,
            mock.patch.object(tools, "_check_subject", return_value=None),
            mock.patch("apps.content.reading.search_passages") as passages,
        ):
            payload = tools._search_passages(mock.Mock(), {"query": "gas concentration", "figures": "only"})
        passages.assert_not_called()
        self.assertEqual(figures.call_args.args[3], tools.FIGURES_ONLY_PER_SEARCH)
        self.assertEqual(payload, {"items": [], "figures": hits})

    def test_the_search_is_advertised_with_both_modes(self):
        description, parameters = tools.SPECIFICATIONS[tools.SEARCH_PASSAGES]
        self.assertEqual(parameters["properties"]["figures"]["enum"], ["also", "only"])


class RicherFigureCitationTests(TestCase):
    """#185 §2: printed label and description at snippet tier; table Markdown and LaTeX at passage tier."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, extraction, _rows, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)
        cls.table = Region.objects.create(
            run=extraction,
            kind=Region.Kind.TABLE,
            page=5,
            seq=1,
            bbox=fixtures.BBOX,
            printed_caption="Table 8.1: Gas concentrations",
            description="A table of CO2 concentrations per chamber.",
            markdown="| a | b |\n|---|---|\n| 1 | 2 |",
            latex="x^2",
        )

    def _complete(self, region, request=None):
        source = {
            "kind": region.kind,
            "entry_id": str(self.entry.pk),
            "page": region.page,
            "region_id": str(region.pk),
        }
        return citations.complete("F1", source, "", request or fixtures.request_for(self.reader))

    def test_a_table_carries_its_label_description_and_markdown(self):
        found = self._complete(self.table)
        self.assertEqual(found["printed_label"], "Table 8.1")
        self.assertEqual(found["description"], "A table of CO2 concentrations per chamber.")
        self.assertEqual(found["markdown"], "| a | b |\n|---|---|\n| 1 | 2 |")
        # LaTeX belongs to equations only.
        self.assertNotIn("latex", found)

    def test_markdown_needs_the_passage_tier(self):
        with mock.patch.object(citations._Access, "allows", side_effect=lambda entry_id, tier: tier != "passage"):
            found = self._complete(self.table)
        self.assertEqual(found["printed_label"], "Table 8.1")
        self.assertNotIn("markdown", found)

    def test_nothing_extra_without_a_snippet(self):
        with mock.patch.object(citations._Access, "allows", return_value=False):
            found = self._complete(self.table)
        for key in ("printed_label", "description", "markdown", "caption"):
            self.assertNotIn(key, found)


class FollowContentLayerTests(TurnBase):
    """#186 §1: prerequisites, cited_by and cites through `follow`."""

    def _context(self, sources):
        ledger = evidence.Ledger(sources)
        return ToolContext(request=fixtures.request_for(self.reader), ledger=ledger, reading=tools.Reading())

    def test_cited_by_lists_publications_with_the_page(self):
        context = self._context({"E1": {"kind": "passage", "entry_id": str(self.entry.pk), "paragraph_id": "p"}})
        found = {
            "entry_id": str(self.entry.pk),
            "cited_by": [
                {"entry_id": "a", "title": "Hydraulics", "page": 210},
                {"entry_id": "a", "title": "Hydraulics", "page": 211},
                {"entry_id": "b", "title": "Pumps", "page": None},
            ],
            "cites": [],
        }
        with mock.patch("apps.extraction.bibliography.citations_of", return_value=found):
            payload = follow.follow(context, {"label": "E1", "relation": "cited_by"})
            cites = follow.follow(context, {"label": "E1", "relation": "cites"})
        self.assertEqual(
            payload["publications"], [{"id": "a", "title": "Hydraulics", "page": 210}, {"id": "b", "title": "Pumps"}]
        )
        self.assertEqual(cites["publications"], [])
        self.assertIn("cites no publication", cites["note"])
        self.assertEqual(activity.summarize(tools.FOLLOW, {"label": "E1"}, payload)["count"], 2)

    def test_prerequisites_label_the_first_paragraph_of_each_section(self):
        bernoulli = self.paragraphs[2]
        context = self._context(
            {"E1": {"kind": "passage", "entry_id": str(self.entry.pk), "paragraph_id": str(bernoulli.pk)}}
        )
        found = [
            {
                "key": "k1",
                "entry_id": str(self.entry.pk),
                "run_id": self.extraction.pk,
                "title": "1 Introduction",
                "first_page": 1,
                "score": 2,
                "later": False,
            },
            {
                "key": "k2",
                "entry_id": "00000000-0000-0000-0000-000000000000",
                "run_id": None,
                "title": "Hidden",
                "first_page": 1,
                "score": 1,
                "later": False,
            },
        ]
        with (
            mock.patch("apps.graph.projection.available", return_value=True),
            mock.patch("apps.graph.queries.visible_entry_ids", return_value=[str(self.entry.pk)]),
            mock.patch("apps.graph.queries.prerequisites", return_value=found) as prerequisites,
        ):
            payload = follow.follow(context, {"label": "E1", "relation": "prerequisites"})
        self.assertEqual(prerequisites.call_args.args[1], [str(self.entry.pk)])
        self.assertEqual([passage["page"] for passage in payload["passages"]], [1])
        self.assertTrue(payload["passages"][0]["label"].startswith("E"))
        self.assertNotIn("later", payload["passages"][0])

    def test_figures_may_only_follow_citations(self):
        context = self._context({"F1": {"kind": "figure", "entry_id": str(self.entry.pk), "region_id": "r"}})
        with mock.patch("apps.extraction.bibliography.citations_of", return_value={"cited_by": [], "cites": []}):
            self.assertEqual(follow.follow(context, {"label": "F1", "relation": "cited_by"})["publications"], [])


class CiteExportTests(TestCase):
    """#186 §2: `POST /api/v1/cite`, the MCP `cite` payload."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.hidden = fixtures.catalog(cls.reader, public=False, name="hidden")
        cls.entry, *_rest = fixtures.publication(cls.reader, cls.open, title="Fluid Mechanics")
        cls.secret, *_rest = fixtures.publication(cls.reader, cls.hidden, title="Secret")

    def _post(self, body):
        return self.client.post("/api/v1/cite", data=json.dumps(body), content_type="application/json")

    def test_a_publication_address_is_cited_as_the_whole_work(self):
        response = self._post({"uris": [f"evilflowers://entry/{self.entry.pk}"], "style": "iso690"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["response"]
        self.assertEqual(data["style"], "iso690")
        self.assertIn("Fluid Mechanics.", data["references"][0]["text"])
        csl = self._post({"uris": [f"evilflowers://entry/{self.entry.pk}"], "style": "csl-json"}).json()["response"]
        self.assertEqual(csl["references"][0]["csl"]["title"], "Fluid Mechanics")

    def test_refusals(self):
        self.assertEqual(self._post({"uris": []}).status_code, 400)
        self.assertEqual(self._post({"uris": ["not an address"]}).status_code, 400)
        self.assertEqual(
            self._post({"uris": [f"evilflowers://entry/{self.entry.pk}"], "style": "mla"}).status_code, 400
        )
        # A hidden publication is not found for this caller, a link to an unknown locator neither.
        self.assertEqual(self._post({"uris": [f"evilflowers://entry/{self.secret.pk}"]}).status_code, 404)
        self.assertEqual(self._post({"uris": [f"/cite/{self.entry.pk}/abcdefgh12"]}).status_code, 404)


class AccessFrameTests(TestCase):
    """#186 §3: an `access` card instead of guessed unlock advice."""

    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, *_rest = fixtures.publication(cls.reader, cls.open, title="Open")
        cls.lent, *_rest = fixtures.publication(cls.reader, cls.open, title="Lent", readium=True)

    def test_once_per_publication_and_only_where_the_text_is_withheld(self):
        request = fixtures.request_for(self.reader)
        sent = set()
        withheld.note(request, self.lent.pk)
        withheld.note(request, self.entry.pk)  # readable: noted by mistake, no card
        frames = withheld.frames(request, sent)
        self.assertEqual([frame["entry_id"] for frame in frames], [str(self.lent.pk)])
        self.assertEqual(frames[0]["title"], "Lent")
        self.assertEqual(frames[0]["reason"], "lcp")
        withheld.note(request, self.lent.pk)
        self.assertEqual(withheld.frames(request, sent), [])

    def test_a_signed_out_reader_who_could_borrow_is_asked_to_sign_in(self):
        request = fixtures.request_for(self.reader)
        request.user = AnonymousUser()
        withheld.note(request, self.lent.pk)
        frames = withheld.frames(request, set())
        if frames and "borrow" in frames[0]["how_to_unlock"]:
            self.assertEqual(frames[0]["how_to_unlock"][0], "sign_in")

    def test_a_budget_cut_is_reported_even_at_passage_tier(self):
        request = fixtures.request_for(self.reader)
        withheld.note(request, self.entry.pk, withheld.BUDGET)
        frames = withheld.frames(request, set())
        self.assertEqual([(frame["entry_id"], frame["reason"]) for frame in frames], [(str(self.entry.pk), "budget")])
