"""IP-033 × IP-034 round trip: the harvesting client harvests Elvira's own OAI-PMH provider.

The only end-to-end check available without a partner server: what the provider writes (marc21, oai_dc,
resumption tokens) is what the OAI-PMH connector reads, through the real HTTP views (Django's test client stands
in for `requests`).
"""

from unittest.mock import MagicMock

from django.test import override_settings

from apps.core.models import Entry
from apps.harvest.adapters.oai import OaiPmhAdapter
from apps.harvest.models import HarvestSource
from apps.harvest.runner import Run
from apps.oai.tests.base import OaiTestCase


class ClientSession:
    """A `requests.Session` stand-in that sends the connector's GETs to the test client."""

    def __init__(self, client):
        self.client = client
        self.headers: dict = {}
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append(params)
        answer = self.client.get("/oai", params)
        response = MagicMock()
        response.status_code = answer.status_code
        response.content = answer.content
        return response


class RoundTripTests(OaiTestCase):
    def setUp(self):
        super().setUp()
        self.originals = [
            self.entry("Elektrické stroje", identifiers={"isbn": "978-80-227-5078-3"}, publisher="STU"),
            self.entry("Teória obvodov", identifiers={"isbn": "8022712345"}, authors=(("Kováč", "Peter"),)),
        ]
        self.sync(*self.originals)

    def harvest(self, prefix: str, **source_fields):
        source = HarvestSource.objects.create(
            slug=f"elvira-{prefix}".replace("_", "-"),
            name="Elvira itself",
            adapter="oai_pmh",
            base_url="https://evilflowers.elvira.stuba.sk/oai",
            metadata_prefix=prefix,
            system_key="arl_stu",
            target_catalog=self.private,
            mode=HarvestSource.Mode.CREATE,
            enabled=True,
            creator=self.owner,
            **source_fields,
        )
        session = ClientSession(self.client)

        def run():
            fresh = HarvestSource.objects.get(pk=source.pk)
            adapter = OaiPmhAdapter(fresh, full=True, session=session)
            return Run(fresh, dry_run=False, full=True, adapter=adapter).execute()

        return source, session, run

    def assert_copies(self):
        copies = {entry.title: entry for entry in Entry.objects.filter(catalog=self.private)}
        self.assertEqual(set(copies), {"Elektrické stroje", "Teória obvodov"})
        first = copies["Elektrické stroje"]
        self.assertEqual(first.identifiers.get("isbn"), "9788022750783")
        self.assertTrue(first.identifiers.get("arl_stu"))
        self.assertEqual(first.language.alpha2, "sk")
        self.assertTrue(first.origin.startswith("harvest:elvira-"))
        self.assertEqual(
            [author.surname for author in copies["Teória obvodov"].authors.order_by("entry_authors__position")],
            ["Kováč"],
        )
        return copies

    def test_marc21_round_trip_creates_once_and_is_idempotent(self):
        _source, session, run = self.harvest("marc21")
        report = run()
        self.assertEqual(report["created"], 2, report)
        self.assertEqual(report["failed"], 0, report)
        copies = self.assert_copies()
        self.assertEqual(copies["Elektrické stroje"].publisher, "STU")
        self.assertEqual(session.calls[0].get("verb"), "Identify")

        again = run()
        self.assertEqual(again["created"], 0, again)
        self.assertEqual(Entry.objects.filter(catalog=self.private).count(), 2)

    def test_oai_dc_round_trip(self):
        _source, _session, run = self.harvest("oai_dc")
        report = run()
        self.assertEqual(report["created"], 2, report)
        self.assert_copies()

    @override_settings(EVILFLOWERS_OAI_PAGE_SIZE=1)
    def test_resumption_tokens_are_followed(self):
        _source, session, run = self.harvest("marc21")
        report = run()
        self.assertEqual(report["created"], 2, report)
        self.assertTrue(any("resumptionToken" in call for call in session.calls))
