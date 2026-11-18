"""#175: content grants as facts, reading budgets and the disclosure log."""

from dataclasses import replace
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.assistant.tests import fixtures as books
from apps.content import budget, disclosure, grants, policies
from apps.content.access import Reason, Surface, decide
from apps.content.models import ContentDisclosure, ContentGrant
from apps.content.tests.fixtures import ALLOWED_RANGES, INSIDE, Scenarios, request_for
from apps.core.models import ApiKey, Catalog, UserAcquisition
from apps.readium.models import License


def _with_states(key: str, states) -> policies.Policy:
    """A policy like `key` whose `grants.license_states` are `states` (bypasses validation on purpose)."""
    policy = policies.require(key)
    document = {**policy.document, "grants": {**policy.grants, "license_states": list(states)}}
    return replace(policy, document=document)


@override_settings(EVILFLOWERS_ALLOWED_IP_RANGES=ALLOWED_RANGES)
class GrantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Scenarios.build(cls)

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)
        self.inside = request_for(self.reader, INSIDE)

    def _license(self, entry, state, **extra):
        now = timezone.now()
        return License.objects.create(
            entry=entry,
            user=self.reader,
            state=state,
            starts_at=now - timedelta(days=10),
            expires_at=now - timedelta(days=3),
            **extra,
        )

    def test_signals_write_a_grant_per_source_and_follow_its_state(self):
        loan = License.objects.get(entry=self.lcp_loan, user=self.reader)
        grant = ContentGrant.objects.get(source="license", source_id=loan.pk)
        self.assertEqual((grant.source_state, grant.entry_id), ("active", self.lcp_loan.pk))
        self.assertIsNotNone(grant.first_opened_at)

        loan.state = License.LicenseState.RETURNED
        loan.save()
        grant.refresh_from_db()
        self.assertEqual(grant.source_state, "returned")

        copy = UserAcquisition.objects.get(user=self.reader, acquisition__entry=self.own_copy)
        self.assertEqual(ContentGrant.objects.get(source_id=copy.pk).source_state, "personal")

    def test_a_deleted_copy_no_longer_grants(self):
        copy = UserAcquisition.objects.get(user=self.reader, acquisition__entry=self.own_copy)
        self.assertEqual(decide(self.inside, self.own_copy).basis, "loan")
        copy_id = copy.pk
        copy.delete()
        self.assertIsNotNone(ContentGrant.objects.get(source_id=copy_id).revoked_at)
        self.assertEqual(decide(self.inside, self.own_copy).basis, "open")

    def test_an_expired_copy_no_longer_grants(self):
        # #215: the grant follows the copy's `expire_at`; an expired own copy unlocks nothing.
        copy = UserAcquisition.objects.get(user=self.reader, acquisition__entry=self.own_copy)
        self.assertEqual(decide(self.inside, self.own_copy).basis, "loan")

        copy.expire_at = timezone.now() - timedelta(minutes=1)
        copy.save()
        self.assertEqual(ContentGrant.objects.get(source_id=copy.pk).expires_at, copy.expire_at)
        self.assertEqual(decide(self.inside, self.own_copy).basis, "open")

        copy.expire_at = timezone.now() + timedelta(days=1)
        copy.save()
        self.assertEqual(decide(self.inside, self.own_copy).basis, "loan")

    def test_cancelled_never_grants(self):
        self._license(self.lcp, License.LicenseState.CANCELLED)
        self.assertNotIn(
            "cancelled", policies.SCHEMA["properties"]["grants"]["properties"]["license_states"]["items"]["enum"]
        )
        # Even a document that names it (written around the schema) does not make a cancelled loan count.
        decision = decide(self.inside, self.lcp, policy=_with_states("open-access", ["cancelled"]))
        self.assertEqual(decision.basis, "none")
        decision = decide(self.inside, self.lcp, policy=_with_states("licensed-standard", ["cancelled"]))
        self.assertEqual((decision.basis, decision.tier), ("none", "metadata"))

    def test_revoked_follows_the_policy(self):
        self._license(self.lcp, License.LicenseState.REVOKED, device_count=1)
        self.assertEqual(decide(self.inside, self.lcp, policy=policies.require("licensed-standard")).basis, "none")
        with_revoked = _with_states("licensed-standard", ["active", "returned", "revoked"])
        decision = decide(self.inside, self.lcp, policy=with_revoked)
        self.assertEqual((decision.basis, decision.tier), ("past_loan", "passage"))

    def test_expired_counts_only_when_opened(self):
        unopened = self._license(self.lcp, License.LicenseState.EXPIRED)
        policy = policies.require("licensed-standard")
        self.assertEqual(decide(self.inside, self.lcp, policy=policy).basis, "none")
        unopened.device_count = 1
        unopened.save()
        self.assertEqual(decide(self.inside, self.lcp, policy=policy).basis, "past_loan")

    def test_a_hidden_entry_stays_not_found_whatever_the_grant(self):
        hidden_lcp = self.hidden
        hidden_lcp.config = {"readium_enabled": True}
        hidden_lcp.save()
        self._license(hidden_lcp, License.LicenseState.RETURNED, device_count=1)
        for policy in policies.all_current():
            with self.subTest(policy=policy.key):
                decision = decide(self.inside, hidden_lcp, policy=policy)
                self.assertFalse(decision.visible)
                self.assertEqual(decision.reason, Reason.HIDDEN)

    def test_switching_the_policy_changes_the_tier_at_once_without_a_backfill(self):
        self.lcp_returned.config = {**self.lcp_returned.config, "content_policy": "licensed-standard"}
        self.lcp_returned.save()
        first = decide(self.inside, self.lcp_returned)
        self.assertEqual((first.basis, first.tier, first.policy), ("past_loan", "passage", "licensed-standard@1"))
        self.lcp_returned.config = {**self.lcp_returned.config, "content_policy": "licensed-strict"}
        self.lcp_returned.save()
        second = decide(self.inside, self.lcp_returned)
        self.assertEqual((second.tier, second.policy), ("metadata", "licensed-strict@1"))

    def test_the_backfill_is_idempotent(self):
        before = ContentGrant.objects.count()
        ContentGrant.objects.all().delete()
        counts = grants.backfill()
        self.assertEqual(ContentGrant.objects.count(), before)
        self.assertEqual(counts["licenses"], License.objects.count())
        grants.backfill()
        self.assertEqual(ContentGrant.objects.count(), before)
        returned = ContentGrant.objects.get(entry=self.lcp_returned)
        self.assertIsNotNone(returned.first_opened_at)

    def test_explain_shows_a_past_loan(self):
        from apps.content import services

        self.lcp_returned.config = {**self.lcp_returned.config, "content_policy": "licensed-standard"}
        self.lcp_returned.save()
        explained = services.explain(self.inside, self.lcp_returned.pk)
        self.assertEqual((explained["basis"], explained["tier"]), ("past_loan", "passage"))
        self.assertEqual(explained["budget"]["percent_of_book"], 10)
        # Not processed into paragraphs: nothing to meter.
        self.assertIsNone(explained["budget_state"])


class BudgetTests(TestCase):
    """A past borrower under `licensed-standard` reads 10 % of the book per 30 days."""

    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.reader = books.user()
        cls.catalog = books.catalog(cls.reader)
        Catalog.objects.filter(pk=cls.catalog.pk).update(content_policy="licensed-standard")
        cls.entry, _run, cls.paragraphs, _venturi, _shear = books.publication(cls.reader, cls.catalog, readium=True)
        now = timezone.now()
        License.objects.create(
            entry=cls.entry,
            user=cls.reader,
            state=License.LicenseState.RETURNED,
            starts_at=now - timedelta(days=20),
            expires_at=now - timedelta(days=6),
            device_count=1,
        )

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)
        self.request = books.request_for(self.reader)
        self.request.META["REMOTE_ADDR"] = INSIDE
        self.total = sum(len(paragraph.text) for paragraph in self.paragraphs)
        self.limit = int(self.total * 10 / 100)

    def test_a_metered_basis_carries_its_budget(self):
        decision = decide(self.request, self.entry)
        self.assertEqual((decision.basis, decision.tier), ("past_loan", "passage"))
        self.assertEqual(budget.book_chars(self.entry.pk), self.total)
        self.assertEqual((decision.budget_state.limit, decision.budget_left), (self.limit, self.limit))
        receipt = decision.receipt(0)
        self.assertEqual((receipt["chars_charged"], receipt["budget_left"]), (0, self.limit))

    def test_passages_are_never_cut_and_exhaustion_drops_to_snippet(self):
        decision = decide(self.request, self.entry, surface=Surface.MCP)
        small = disclosure.Item(chars=self.limit - 5, locator="a")
        too_big = disclosure.Item(chars=10, locator="b")
        fits = disclosure.Item(chars=5, locator="c")
        charged = disclosure.record(self.request, decision, [small, too_big, fits])
        self.assertEqual([item.locator for item in charged.kept], ["a", "c"])
        self.assertEqual([item.locator for item in charged.left_out], ["b"])
        self.assertEqual((charged.chars_charged, charged.budget_left), (self.limit, 0))
        self.assertEqual(ContentDisclosure.objects.filter(entry=self.entry).count(), 2)

        after = decide(self.request, self.entry, surface=Surface.MCP)
        self.assertEqual((after.tier, after.reason), ("snippet", "budget"))
        self.assertTrue(after.allows("snippet"))
        self.assertFalse(after.allows("passage"))

    def test_the_window_forgets_old_reads(self):
        decision = decide(self.request, self.entry)
        disclosure.record(self.request, decision, [disclosure.Item(chars=self.limit)])
        ContentDisclosure.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.assertEqual(decide(self.request, self.entry).budget_left, self.limit)

    def test_prune_keeps_rows_inside_the_budget_window(self):
        decision = decide(self.request, self.entry)
        disclosure.record(self.request, decision, [disclosure.Item(chars=3), disclosure.Item(chars=4)])
        first, second = ContentDisclosure.objects.order_by("chars")
        ContentDisclosure.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=20))
        ContentDisclosure.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(days=400))
        self.assertEqual(disclosure.prune(), 1)
        self.assertEqual(list(ContentDisclosure.objects.values_list("chars", flat=True)), [3])

    def test_the_assistant_withholds_a_passage_that_does_not_fit(self):
        from apps.assistant.library import Text, ToolContext

        text = Text.of(ToolContext(request=self.request), self.entry)
        self.assertIsNotNone(text.decision)
        handed = [text.passage(paragraph) for paragraph in self.paragraphs]
        charged = sum(len(passage["text"]) for passage in handed if passage)
        self.assertLessEqual(charged, self.limit)
        self.assertIn(None, handed)
        self.assertEqual(
            sum(ContentDisclosure.objects.filter(entry=self.entry).values_list("chars_charged", flat=True)), charged
        )


class DisclosureLogTests(TestCase):
    """Unmetered reads are logged for machine callers only, when the policy keeps a log; parity's own surfaces
    write nothing for the in-app assistant."""

    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.reader = books.user()
        cls.catalog = books.catalog(cls.reader)
        cls.entry, *_rest = books.publication(cls.reader, cls.catalog)

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)

    def test_an_api_key_caller_is_logged_and_the_assistant_is_not(self):
        request = books.request_for(self.reader)
        request.api_key = ApiKey.objects.create(user=self.reader, name="agent", processor="external")
        decision = decide(request, self.entry, surface=Surface.MCP)
        self.assertFalse(decision.metered)
        with self.assertLogs("apps.content.disclosure", "INFO") as logged:
            disclosure.record(request, decision, [disclosure.Item(chars=12, locator="k")])
        row = ContentDisclosure.objects.get()
        self.assertEqual((row.processor, row.chars, row.chars_charged, row.policy), ("external", 12, 0, "parity@1"))
        self.assertIn('"event": "content.disclosure"', logged.output[0])

        assistant = decide(books.request_for(self.reader), self.entry, surface=Surface.ASSISTANT)
        disclosure.record(books.request_for(self.reader), assistant, [disclosure.Item(chars=12)])
        self.assertEqual(ContentDisclosure.objects.count(), 1)
