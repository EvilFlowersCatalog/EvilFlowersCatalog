"""#172: under `parity` every surface returns exactly what it returned before the gate."""

from django.test import TestCase, override_settings

from apps.assistant.pdf import IP_BLOCKED, NO_PDF, READIUM, DocumentUnavailable, open_access_document
from apps.content.access import Reason, Surface, Tier, decide, decide_many
from apps.content.tests.fixtures import ALLOWED_RANGES, INSIDE, OUTSIDE, Scenarios, request_for
from apps.core.errors import ProblemDetailException
from apps.extraction.discovery import EntryFigures

#: name → (text readable outside the reader, `open_access_document` refusal, `/figures` status)
#: `None` as the refusal means the document check passed and only the missing PDF stopped it.
EXPECTED_OUTSIDE = {
    "open": (True, NO_PDF, 200),
    "lcp": (False, READIUM, 200),
    "lcp_loan": (False, READIUM, 200),
    "lcp_returned": (False, READIUM, 200),
    "ip": (False, IP_BLOCKED, 403),
    # LCP is worded first by the document check, the IP block first by `/figures` — both stay as they were.
    "lcp_ip": (False, READIUM, 403),
    "own_copy": (True, NO_PDF, 200),
    "own_copy_lcp": (False, READIUM, 200),
}


def _refusal(request, entry):
    try:
        open_access_document(request, entry)
    except DocumentUnavailable as error:
        return error.args[0]
    return None


def _figures_status(request, entry):
    try:
        EntryFigures()._open_run(request, entry.pk)
    except ProblemDetailException as error:
        return int(error.status)
    return 200


@override_settings(EVILFLOWERS_ALLOWED_IP_RANGES=ALLOWED_RANGES)
class ParityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Scenarios.build(cls)

    def _check(self, ip, expected):
        request = request_for(self.reader, ip)
        for name, (readable, refusal, figures) in expected.items():
            entry = getattr(self, name)
            with self.subTest(entry=name, ip=ip):
                decision = decide(request, entry)
                self.assertEqual(decision.policy, "parity@1")
                self.assertEqual(decision.allows(Tier.FULL), readable)
                self.assertEqual(decision.allows(Tier.OUTLINE), readable)
                self.assertEqual(decision.allows(Tier.SNIPPET), readable)
                self.assertEqual(_refusal(request, entry), refusal)
                self.assertEqual(_figures_status(request, entry), figures)

    def test_from_outside_the_ip_ranges(self):
        self._check(OUTSIDE, EXPECTED_OUTSIDE)

    def test_from_inside_the_ip_ranges(self):
        inside = dict(EXPECTED_OUTSIDE)
        inside["ip"] = (True, NO_PDF, 200)
        inside["lcp_ip"] = (False, READIUM, 200)
        self._check(INSIDE, inside)

    def test_a_hidden_entry_is_not_found(self):
        request = request_for(self.reader, INSIDE)
        decision = decide(request, self.hidden)
        self.assertFalse(decision.visible)
        self.assertEqual(decision.reason, Reason.HIDDEN)
        self.assertFalse(decision.allows(Tier.METADATA))
        self.assertEqual(_figures_status(request, self.hidden), 404)

    def test_an_lcp_loan_stays_at_metadata_and_says_why(self):
        decision = decide(request_for(self.reader, INSIDE), self.lcp_loan)
        self.assertEqual((decision.basis, decision.tier, decision.reason), ("none", "metadata", Reason.LCP))
        self.assertEqual(decision.receipt()["policy"], "policy:parity@1")

    def test_the_ip_cap_names_itself(self):
        decision = decide(request_for(self.reader, OUTSIDE), self.ip)
        self.assertEqual((decision.basis, decision.tier, decision.reason), ("open", "metadata", Reason.OUTSIDE_IP))
        self.assertIn("campus_network", decision.how_to_unlock)
        with self.assertRaises(ProblemDetailException):
            decision.raise_outside_ip(Tier.PASSAGE)
        decision.raise_outside_ip(Tier.METADATA)  # the catalogue record is never IP-blocked

    def test_a_catalog_member_is_still_ip_blocked_under_parity(self):
        self.assertFalse(decide(request_for(self.member, OUTSIDE), self.ip).allows(Tier.SNIPPET))

    def test_parity_does_not_cap_api_keys_or_the_assistant(self):
        request = request_for(self.reader, INSIDE)
        self.assertTrue(decide(request, self.open, surface=Surface.ASSISTANT).allows(Tier.FULL))
        self.assertTrue(decide(request, self.open, processor="external").allows(Tier.FULL))

    def test_a_batch_decides_like_one_by_one(self):
        request = request_for(self.reader, OUTSIDE)
        entries = [getattr(self, name) for name in (*EXPECTED_OUTSIDE, "hidden")]
        batch = decide_many(request, entries)
        for entry in entries:
            single = decide(request, entry)
            self.assertEqual((batch[str(entry.pk)].tier, batch[str(entry.pk)].visible), (single.tier, single.visible))

    def test_a_batch_reads_the_policy_map_and_the_default_once(self):
        from unittest import mock

        from apps.core import settings_store

        request = request_for(self.reader, OUTSIDE)
        entries = [getattr(self, name) for name in EXPECTED_OUTSIDE]
        with mock.patch.object(settings_store, "get", wraps=settings_store.get) as read:
            decide_many(request, entries)
        self.assertEqual([call.args[0] for call in read.call_args_list], ["content.default_policy"])

    def test_a_batch_costs_a_fixed_number_of_queries(self):
        request = request_for(self.reader, OUTSIDE)
        entries = [getattr(self, name) for name in EXPECTED_OUTSIDE]
        decide_many(request, entries[:2])  # warm the policy cache
        with self.assertNumQueries(4):  # visibility, catalogs, licences, copies
            decide_many(request, entries)
