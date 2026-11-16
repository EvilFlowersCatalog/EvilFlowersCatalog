"""#207: records no identifier matched get a suggested entry from the decider; a librarian links or creates."""

from unittest.mock import patch

from django.test import override_settings

from apps.core.models import Entry
from apps.decider import client
from apps.harvest.models import HarvestRecord, HarvestSource, MatchSuggestion
from apps.harvest.records import SourceAuthor, SourceRecord
from apps.harvest.runner import run_source
from apps.harvest.suggest import search_candidates
from apps.harvest.tests.base import HarvestTestCase
from apps.harvest.tests.test_api import ApiTestCase
from apps.harvest.tests.test_runs import ListAdapter

ASK = "apps.decider.client.ask"
DECIDER = override_settings(EVILFLOWERS_DECIDER_URL="http://decider.test", EVILFLOWERS_DECIDER_MODEL="multilingual")


def thesis(record_id="T1", **kwargs) -> SourceRecord:
    values = {
        "record_id": record_id,
        "title": "Numerické metódy riešenia diferenciálnych rovníc: diplomová práca",
        "identifiers": {},
        "language": "sk",
        "published": "2011",
        "authors": [SourceAuthor("Kováč", "Ján")],
    }
    values.update(kwargs)
    return SourceRecord(**values)


def answering(choice_of):
    """A fake `client.ask` that answers the first candidate (or `none`) and records the criteria."""
    calls = []

    def ask(states, questions):
        criteria = questions["match"]["criteria"]
        calls.append(criteria)
        picked = choice_of(criteria)
        return [{"match": client.Answer(picked, 0.83, {picked: 0.83})} for _ in states], "multilingual@r1"

    ask.calls = calls
    return ask


def first(criteria):
    return next(iter(criteria))


@DECIDER
class SuggestionRunTests(HarvestTestCase):
    def setUp(self):
        self.held = self.entry("Numerické metódy riešenia diferenciálnych rovníc", published_at="2010")
        self.entry("Organická chémia", published_at="2011")

    def run_records(self, records, mode=HarvestSource.Mode.CREATE):
        self.harvest_source = self.source(mode=mode)
        return run_source(self.harvest_source, adapter=ListAdapter(records))

    def test_candidates_by_title_and_year(self):
        self.entry("Numerické metódy", published_at="1990")
        found = search_candidates(self.catalog, thesis(), 5)
        self.assertEqual(found[0], str(self.held.pk))
        self.assertEqual(len(found), 1, "1990 is outside year ± 1, chemistry shares no word")

    def test_suggested_record_is_held_not_created_or_linked(self):
        ask = answering(first)
        with patch(ASK, side_effect=ask):
            report = self.run_records([thesis()])

        self.assertEqual(report["suggested"], 1, report)
        self.assertEqual(report["created"], 0)
        row = HarvestRecord.objects.get(record_id="T1")
        self.assertEqual(row.status, HarvestRecord.Status.SUGGESTED)
        self.assertIsNone(row.entry_id)
        self.held.refresh_from_db()
        self.assertNotIn("arl_stu", self.held.identifiers or {})
        suggestion = MatchSuggestion.objects.get(record=row)
        self.assertEqual(
            (suggestion.entry_id, suggestion.status, suggestion.p, suggestion.decider_model),
            (self.held.pk, "pending", 0.83, "multilingual@r1"),
        )
        self.assertIn("none", ask.calls[0])
        self.assertIn("Numerické metódy riešenia diferenciálnych rovníc / ", ask.calls[0][str(self.held.pk)])

    def test_second_run_does_not_ask_again(self):
        with patch(ASK, side_effect=answering(first)):
            self.run_records([thesis()])
        with patch(ASK) as ask:
            report = run_source(self.harvest_source, adapter=ListAdapter([thesis()]))
        ask.assert_not_called()
        self.assertEqual(report["suggested"], 1)
        self.assertEqual(MatchSuggestion.objects.count(), 1)

    def test_none_answer_creates_as_today(self):
        with patch(ASK, side_effect=answering(lambda criteria: "none")):
            report = self.run_records([thesis()])
        self.assertEqual(report["created"], 1, report)
        self.assertFalse(MatchSuggestion.objects.exists())

    def test_decider_error_creates_as_today_and_stops_asking(self):
        with patch(ASK, side_effect=client.DeciderError("down")) as ask:
            report = self.run_records([thesis(), thesis("T2", title="Numerické metódy II")])
        self.assertEqual(report["created"], 2, report)
        self.assertEqual(ask.call_count, 1)

    @override_settings(EVILFLOWERS_DECIDER_URL="")
    def test_without_decider_nothing_changes(self):
        with patch(ASK) as ask:
            report = self.run_records([thesis()])
        ask.assert_not_called()
        self.assertEqual(report["created"], 1)

    def test_dry_run_does_not_ask(self):
        self.harvest_source = self.source(mode=HarvestSource.Mode.CREATE)
        with patch(ASK) as ask:
            run_source(self.harvest_source, adapter=ListAdapter([thesis()]), dry_run=True)
        ask.assert_not_called()

    def test_ambiguous_identifier_offers_the_entries_sharing_it(self):
        a = self.entry("Fyzika I", identifiers={"isbn": "9788022712347"})
        b = self.entry("Fyzika II", identifiers={"isbn": "80-227-1234-5"})
        ask = answering(lambda criteria: str(b.pk))
        with patch(ASK, side_effect=ask):
            report = self.run_records([thesis(identifiers={"isbn": "9788022712347"})])
        self.assertEqual(report["suggested"], 1, report)
        self.assertEqual(set(ask.calls[0]) - {"none"}, {str(a.pk), str(b.pk)})

    def test_identifier_match_later_supersedes_the_pending_suggestion(self):
        with patch(ASK, side_effect=answering(first)):
            self.run_records([thesis()], mode=HarvestSource.Mode.LINK_ONLY)
        Entry.objects.filter(pk=self.held.pk).update(identifiers={"isbn": "9788022712347"})
        report = run_source(self.harvest_source, adapter=ListAdapter([thesis(identifiers={"isbn": "9788022712347"})]))
        self.assertEqual(report["matched"], 1, report)
        self.assertEqual(MatchSuggestion.objects.get().status, MatchSuggestion.Status.SUPERSEDED)


@DECIDER
class SuggestionApiTests(ApiTestCase):
    def setUp(self):
        self.held = self.entry("Numerické metódy riešenia diferenciálnych rovníc", published_at="2010")

    def suggested(self, mode):
        self.harvest_source = self.source(mode=mode)
        with patch(ASK, side_effect=answering(first)):
            run_source(self.harvest_source, adapter=ListAdapter([thesis()]))
        return MatchSuggestion.objects.get()

    def list(self, user):
        response = self.client.get("/api/v1/harvest/match-suggestions", headers=self.headers(user))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]

    def decide(self, suggestion, verdict):
        return self.client.post(
            f"/api/v1/harvest/match-suggestions/{suggestion.pk}/{verdict}", headers=self.headers(self.manager)
        )

    def test_list_shows_pending_suggestions_to_managers_only(self):
        suggestion = self.suggested(HarvestSource.Mode.CREATE)
        items = self.list(self.manager)
        self.assertEqual([item["id"] for item in items], [str(suggestion.pk)])
        self.assertEqual(items[0]["source_record_id"], "T1")
        self.assertEqual(items[0]["entry_title"], self.held.title)
        self.assertEqual(self.list(self.outsider), [])

    def test_accept_links_the_record(self):
        suggestion = self.suggested(HarvestSource.Mode.LINK_ONLY)
        response = self.decide(suggestion, "accept")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["response"]["status"], "accepted")
        self.held.refresh_from_db()
        self.assertEqual(self.held.identifiers["arl_stu"], "T1")
        row = HarvestRecord.objects.get(record_id="T1")
        self.assertEqual((row.status, row.entry_id, row.matched_on), ("matched", self.held.pk, "decider"))
        self.assertEqual(self.decide(suggestion, "accept").status_code, 409)

    def test_reject_creates_a_new_entry(self):
        suggestion = self.suggested(HarvestSource.Mode.CREATE)
        response = self.decide(suggestion, "reject")
        self.assertEqual(response.status_code, 200, response.content)
        row = HarvestRecord.objects.get(record_id="T1")
        self.assertEqual(row.status, HarvestRecord.Status.CREATED, row.message)
        self.assertNotEqual(row.entry_id, self.held.pk)
        # Not suggested again: the record now holds its own entry.
        with patch(ASK) as ask:
            report = run_source(self.harvest_source, adapter=ListAdapter([thesis()]))
        ask.assert_not_called()
        self.assertEqual(report["unchanged"], 1, report)

    def test_reject_in_link_only_mode_leaves_the_record_unmatched_and_unasked(self):
        suggestion = self.suggested(HarvestSource.Mode.LINK_ONLY)
        self.assertEqual(self.decide(suggestion, "reject").status_code, 200)
        with patch(ASK) as ask:
            report = run_source(self.harvest_source, adapter=ListAdapter([thesis()]))
        ask.assert_not_called()
        self.assertEqual(report["unmatched"], 1, report)
