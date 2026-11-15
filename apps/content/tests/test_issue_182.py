"""#182: the #159 figure rule under the content gate, one reading rule for assistant and MCP, packs in support."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.assistant.tests import fixtures as books
from apps.content import disclosure, policies, reading
from apps.content.access import Disclosure, Surface, decide
from apps.content.models import ContentDisclosure
from apps.core.models import ApiKey
from apps.extraction import locators
from apps.extraction.discovery import may_open_region
from apps.readium.models import License


def _without_region_rule(key: str) -> policies.Policy:
    policy = policies.require(key)
    return replace(policy, document={**policy.document, "grants": {**policy.grants, "regions_on_live_license": False}})


class FigureRuleTests(TestCase):
    """A live LCP loan shows the borrower captions and crops (#159), now as the gate decides for regions."""

    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.reader = books.user()
        cls.stranger = books.user("stranger")
        cls.catalog = books.catalog(cls.reader)
        cls.lcp, *_rest = books.publication(cls.reader, cls.catalog, title="Lent", readium=True)
        cls.open, *_rest = books.publication(cls.reader, cls.catalog, title="Open")
        now = timezone.now()
        License.objects.create(
            entry=cls.lcp,
            user=cls.reader,
            state=License.LicenseState.ACTIVE,
            starts_at=now,
            expires_at=now + timedelta(days=7),
        )

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)

    def test_parity_keeps_the_borrower_rule_for_regions_only(self):
        borrower = books.request_for(self.reader)
        text = decide(borrower, self.lcp)
        regions = decide(borrower, self.lcp, regions=True)
        self.assertEqual((text.tier, text.basis), ("metadata", "none"))
        self.assertEqual((regions.tier, regions.basis), ("snippet", "loan"))
        self.assertTrue(may_open_region(borrower, self.lcp))
        self.assertFalse(may_open_region(books.request_for(self.stranger), self.lcp))
        self.assertTrue(may_open_region(books.request_for(self.stranger), self.open))

    def test_a_policy_can_turn_the_rule_off(self):
        borrower = books.request_for(self.reader)
        self.assertFalse(
            decide(borrower, self.lcp, regions=True, policy=_without_region_rule("parity")).allows("snippet")
        )
        schema = policies.SCHEMA["properties"]["grants"]
        self.assertIn("regions_on_live_license", schema["properties"])
        self.assertNotIn("regions_on_live_license", schema["required"])

    def test_the_region_grant_never_exceeds_snippet_or_the_loan_tier(self):
        borrower = books.request_for(self.reader)
        self.assertEqual(decide(borrower, self.lcp, regions=True, policy=policies.require("open-access")).tier, "full")
        strict = policies.require("licensed-strict")
        external = decide(borrower, self.lcp, regions=True, policy=strict, processor="external")
        self.assertEqual((external.tier, external.reason), ("metadata", "processor"))

    def test_figure_search_follows_the_gate(self):
        from apps.extraction import regions

        Region = self.lcp.acquisitions.first().extraction_runs.first().regions
        Region.update(embedding_text="Venturi tube pressure drop")
        self.open.acquisitions.first().extraction_runs.first().regions.update(embedding_text="Venturi tube")
        borrower = regions.search(books.request_for(self.reader), "Venturi", licensed=True, limit=10)
        stranger = regions.search(books.request_for(self.stranger), "Venturi", licensed=True, limit=10)
        self.assertIn(str(self.lcp.pk), {hit.entry_id for hit in borrower.hits})
        self.assertNotIn(str(self.lcp.pk), {hit.entry_id for hit in stranger.hits})
        assistant = regions.search(books.request_for(self.reader), "Venturi", licensed=False, limit=10)
        self.assertNotIn(str(self.lcp.pk), {hit.entry_id for hit in assistant.hits})


class InOrderTests(SimpleTestCase):
    def _decision(self, left: int) -> Disclosure:
        state = SimpleNamespace(left=left, payload=lambda: {})
        return Disclosure(
            entry_id="e",
            visible=True,
            tier="passage",
            basis="past_loan",
            reason="",
            how_to_unlock=[],
            policy="licensed-standard@1",
            policy_source="catalog",
            budget={},
            budget_state=state,
            surface=Surface.MCP,
        )

    def test_reading_order_stops_at_the_first_misfit(self):
        items = [disclosure.Item(chars=5, locator="a"), disclosure.Item(chars=50), disclosure.Item(chars=3)]
        request = SimpleNamespace(user=None)
        ordered = disclosure.record(request, self._decision(10), items, in_order=True, dry_run=True)
        self.assertEqual([item.chars for item in ordered.kept], [5])
        self.assertEqual([item.chars for item in ordered.left_out], [50, 3])
        loose = disclosure.record(request, self._decision(10), items, dry_run=True)
        self.assertEqual([item.chars for item in loose.kept], [5, 3])


class SharedReadingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.reader = books.user()
        cls.catalog = books.catalog(cls.reader)
        cls.entry, cls.entry_run, cls.paragraphs, *_rest = books.publication(cls.reader, cls.catalog)
        cls.lcp, cls.lcp_run, *_rest = books.publication(cls.reader, cls.catalog, title="Lent", readium=True)
        locators.assign(cls.entry_run)
        locators.assign(cls.lcp_run)

    def setUp(self):
        policies.invalidate()
        self.addCleanup(policies.invalidate)

    def test_assistant_and_mcp_share_the_passage_search_and_its_logging(self):
        from apps.assistant import tools

        request = books.request_for(self.reader)
        payload = tools._search_passages(request, {"query": "Bernoulli pressure speed"})
        self.assertTrue(payload["items"])
        self.assertFalse(ContentDisclosure.objects.exists())  # in-app, unmetered: not logged, as before

        found = reading.search_passages(
            request, "Bernoulli pressure speed", limit=5, per_entry=2, surface=Surface.ASSISTANT
        )
        passage = next(p for item in found.items for p in item.passages if p.text)
        self.assertTrue(passage.locator_key)
        self.assertFalse(ContentDisclosure.objects.exists())

        agent = books.request_for(self.reader)
        agent.api_key = ApiKey.objects.create(user=self.reader, name="agent")
        reading.search_passages(agent, "Bernoulli pressure speed", limit=5, per_entry=2)
        row = ContentDisclosure.objects.filter(entry=self.entry).first()
        self.assertEqual((row.chars_charged, row.processor, row.surface), (0, "external", "mcp"))
        self.assertIn(passage.locator_key, set(ContentDisclosure.objects.values_list("locator", flat=True)))
        self.assertFalse(ContentDisclosure.objects.filter(entry=self.lcp).exists())

    def test_assistant_citations_carry_the_citation_address(self):
        from apps.assistant import citations

        paragraph = self.entry_run.paragraphs.order_by("seq")[2]
        source = {
            "kind": "passage",
            "entry_id": str(self.entry.pk),
            "page": paragraph.page,
            "paragraph_id": str(paragraph.pk),
            "locator_key": paragraph.locator_key,
        }
        cited = citations.complete("E1", source, "Pressure falls", books.request_for(self.reader))
        self.assertEqual(cited["uri"], f"evilflowers://entry/{self.entry.pk}/locator/{paragraph.locator_key}")
        self.assertTrue(cited["url"].endswith(f"/cite/{self.entry.pk}/{paragraph.locator_key}"))


class _Client:
    """A support checker that calls everything supported."""

    def chat(self, messages, tools=None, response_format=None):
        yield SimpleNamespace(content='{"verdict": "supported", "score": 90}', done=True, total_tokens=10)


class PackSupportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        policies.ensure_presets()
        cls.agent_user = books.user()
        cls.checker = books.user("checker")
        cls.catalog = books.catalog(cls.agent_user)
        cls.entry, cls.entry_run, *_rest = books.publication(cls.agent_user, cls.catalog)
        locators.assign(cls.entry_run)

    def test_an_agents_answer_is_checked_against_its_pack_with_the_checkers_access(self):
        from apps.assistant import support

        pack = reading.pack(books.request_for(self.agent_user), "Bernoulli pressure speed", budget_chars=2000)
        self.assertTrue(pack["passages"])
        answer = "Pressure falls where the speed rises [E1]. Nothing cites this sentence."
        checker = books.request_for(self.checker)
        ledger = support.ledger_for_pack(checker, pack["pack_id"])
        self.assertIn("E1", ledger.texts)
        self.assertEqual(ledger.sources["E1"]["locator_key"], pack["passages"][0]["uri"].rsplit("/", 1)[-1])

        verdicts = support.verify_pack(checker, pack["pack_id"], answer, client=_Client())
        self.assertEqual([(verdict.label, verdict.verdict) for verdict in verdicts], [("E1", "supported")])
        with self.assertRaises(support.UnknownPack):
            support.ledger_for_pack(checker, "0" * 20)

    def test_a_label_the_checker_may_not_read_has_no_text(self):
        from apps.assistant import support
        from apps.core.models import Catalog

        pack = reading.pack(books.request_for(self.agent_user), "Bernoulli pressure speed", budget_chars=2000)
        self.entry.config = {**self.entry.config, "readium_enabled": True}
        self.entry.save()
        Catalog.objects.filter(pk=self.catalog.pk).update(content_policy="parity")
        ledger = support.ledger_for_pack(books.request_for(self.checker), pack["pack_id"])
        self.assertIn("E1", ledger.sources)
        self.assertNotIn("E1", ledger.texts)
        self.assertEqual(
            support.verify_pack(books.request_for(self.checker), pack["pack_id"], "X [E1].", _Client()), []
        )
