"""#159: figure and table search across the library, its crops, and the MCP tool.

Four publications: "Fluid Mechanics" (open), "Secret" (hidden catalog), "Licensed" (Readium LCP) and a figure of
each with the same caption. Word matching only (the query is not embedded here), so the ranking is deterministic.
"""

import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.assistant.tests import fixtures
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey
from apps.extraction import references, regions
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry
from apps.readium.models import License

QUERY = "Venturiho trubica"


class FakeStorage:
    def exists(self, name):
        return True

    def open(self, name, mode="rb"):
        return io.BytesIO(b"RIFF0000WEBP")


class FigureFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.borrower = fixtures.user("borrower")
        cls.open = fixtures.catalog(cls.reader)
        cls.hidden = fixtures.catalog(cls.reader, public=False, name="hidden")
        cls.entry, run, _rows, cls.venturi, _shear = fixtures.publication(cls.reader, cls.open)
        cls.secret, secret_run, _r, cls.secret_venturi, _s = fixtures.publication(
            cls.reader, cls.hidden, title="Secret"
        )
        cls.licensed, licensed_run, _r2, cls.licensed_venturi, _s2 = fixtures.publication(
            cls.reader, cls.open, title="Licensed", readium=True
        )
        for extraction in (run, secret_run, licensed_run):
            references.build(extraction)
            with mock.patch("apps.core.search.embeddings.enabled", return_value=False):
                regions.index_run(extraction)
        now = timezone.now()
        License.objects.create(
            entry=cls.licensed,
            user=cls.borrower,
            state=License.LicenseState.ACTIVE,
            starts_at=now,
            expires_at=now + timedelta(days=7),
        )

    def setUp(self):
        patcher = mock.patch("apps.core.search.embeddings.try_embed_query", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)


class FigureSearchTests(FigureFixture):
    auth: dict = {}

    def login(self, user):
        key = ApiKey.objects.create(user=user, name="test")
        self.auth = {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def get(self, url, params=None):
        return self.client.get(url, params or {}, headers=self.auth)

    def search(self, query=QUERY, status=200, **params):
        response = self.get("/api/v1/search", {"q": query, **params})
        self.assertEqual(response.status_code, status, response.content[:400])
        return response.json()["response"] if status == 200 else response

    def test_finds_the_figure_with_its_crop(self):
        found = self.search()
        self.assertTrue(found["degraded"])
        self.assertEqual([item["region_id"] for item in found["items"]], [str(self.venturi.pk)])
        hit = found["items"][0]
        self.assertEqual(hit["entry_id"], str(self.entry.pk))
        self.assertEqual(hit["title"], "Fluid Mechanics")
        self.assertEqual((hit["page"], hit["kind"], hit["matched"]), (4, "figure", ["words"]))
        self.assertIn("Venturiho trubica", hit["caption"])
        self.assertTrue(hit["crop_url"].endswith(f"/data/v1/entries/{self.entry.pk}/regions/{self.venturi.pk}.webp"))
        self.assertTrue(hit["downloadable"])
        self.assertGreater(hit["score"], 0)

    def test_a_hidden_publication_is_never_returned(self):
        for found in (self.search(), self.search(kind="figure,table"), self.search(catalog_id=str(self.hidden.pk))):
            self.assertNotIn(str(self.secret.pk), {item["entry_id"] for item in found["items"]})
        self.assertEqual(self.search(catalog_id=str(self.hidden.pk))["items"], [])

    def test_kind(self):
        self.assertEqual(self.search(kind="table")["items"], [])
        self.assertEqual(len(self.search(kind="figure")["items"]), 1)
        self.search(kind="equation", status=400)
        self.search(query="", status=400)

    def test_downloadable_respects_print_and_share(self):
        self.entry.config = {"evilflowers_viewer_print": False}
        self.entry.save()
        hit = self.search()["items"][0]
        self.assertIsNotNone(hit["crop_url"])
        self.assertFalse(hit["downloadable"])

    def test_a_licensed_title_only_for_a_borrower(self):
        anonymous = {item["entry_id"] for item in self.search()["items"]}
        self.assertNotIn(str(self.licensed.pk), anonymous)

        self.login(self.borrower)
        found = self.search()
        self.assertEqual({item["entry_id"] for item in found["items"]}, {str(self.entry.pk), str(self.licensed.pk)})
        licensed = next(item for item in found["items"] if item["entry_id"] == str(self.licensed.pk))
        self.assertFalse(licensed["downloadable"])

    def test_the_crop_of_a_licensed_title_is_403_without_a_loan(self):
        url = f"/data/v1/entries/{self.licensed.pk}/regions/{self.licensed_venturi.pk}.webp"
        self.assertEqual(self.get(url).status_code, 403)
        self.login(self.reader)
        self.assertEqual(self.get(url).status_code, 403)

        self.login(self.borrower)
        with mock.patch("apps.extraction.discovery.get_storage", return_value=FakeStorage()):
            response = self.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, max-age=3600")

    def test_the_crop_of_an_open_title_and_of_a_hidden_one(self):
        with mock.patch("apps.extraction.discovery.get_storage", return_value=FakeStorage()):
            response = self.client.get(f"/data/v1/entries/{self.entry.pk}/regions/{self.venturi.pk}.webp")
            self.assertEqual(response.status_code, 200)
            hidden = self.client.get(f"/data/v1/entries/{self.secret.pk}/regions/{self.secret_venturi.pk}.webp")
            self.assertEqual(hidden.status_code, 404)

    def test_the_assistant_still_leaves_licensed_titles_out(self):
        request = fixtures.request_for(self.borrower)
        hits = regions.search_regions(request, QUERY, limit=5)
        self.assertEqual([hit.entry_id for hit in hits], [str(self.entry.pk)])


class SearchFiguresToolTests(FigureFixture):
    def call(self, arguments, user):
        request = RequestFactory().post("/mcp/v1")
        request.user = user
        response = McpServer(registry).handle(
            request,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_figures", "arguments": arguments},
            },
            LATEST_PROTOCOL_VERSION,
        )
        return response["result"]

    def test_tool(self):
        result = self.call({"query": QUERY}, AnonymousUser())
        self.assertFalse(result.get("isError"), result)
        items = result["structuredContent"]["items"]
        self.assertEqual([item["region_id"] for item in items], [str(self.venturi.pk)])
        self.assertTrue(items[0]["crop_url"].startswith("http"))

        borrowed = self.call({"query": QUERY, "kind": ["figure"]}, self.borrower)["structuredContent"]["items"]
        self.assertIn(str(self.licensed.pk), {item["entry_id"] for item in borrowed})
        self.assertNotIn(str(self.secret.pk), {item["entry_id"] for item in borrowed})

        scoped = self.call({"query": QUERY, "catalog_id": str(self.hidden.pk)}, AnonymousUser())
        self.assertEqual(scoped["structuredContent"]["items"], [])

    def test_bad_arguments(self):
        self.assertTrue(self.call({"query": QUERY, "kind": ["equation"]}, AnonymousUser()).get("isError"))
        self.assertTrue(self.call({}, AnonymousUser()).get("isError"))
