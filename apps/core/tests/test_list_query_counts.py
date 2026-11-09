"""List endpoints cost a fixed number of queries, whatever the page size (#124).

Each list endpoint that embeds entries is called with N rows, then with 2N; the number of
queries must not change. The fixture gives every entry the relations the serializer reads:
two authors, a category, an acquisition and two feeds, each of which has a parent and a child.
Before #124 `GET /api/v1/entries` issued 226 SELECTs for 30 entries and the licence list 646.
"""

import datetime
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import (
    Acquisition,
    ApiKey,
    Author,
    AuthSource,
    Catalog,
    Category,
    Entry,
    EntryAuthor,
    Feed,
    ShelfRecord,
    User,
    UserAcquisition,
    UserActivity,
)
from apps.readium.models import License, Reservation

PAGE = 3


class ListQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="root", name="Root", surname="T", auth_source=source, is_superuser=True)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.catalog = Catalog.objects.create(creator=cls.user, title="Main", url_name="main", is_public=True)

        def feed(slug):
            return Feed.objects.create(
                catalog=cls.catalog,
                creator=cls.user,
                title={"en": slug},
                url_name=slug,
                kind=Feed.FeedKind.ACQUISITION,
                content="x",
            )

        cls.entries = []
        for i in range(2 * PAGE):
            entry = Entry.objects.create(creator=cls.user, catalog=cls.catalog, title=f"Book {i}")
            for position in range(2):
                author = Author.objects.create(catalog=cls.catalog, name=f"A{i}", surname=f"S{position}")
                EntryAuthor.objects.create(entry=entry, author=author, position=position)
            category = Category.objects.create(
                catalog=cls.catalog, creator=cls.user, term=f"t{i}", label={"en": f"t{i}"}
            )
            entry.categories.add(category)
            acquisition = Acquisition.objects.create(
                entry=entry, mime="application/pdf", file_url=f"https://example.org/{i}.pdf"
            )
            UserAcquisition.objects.create(user=cls.user, acquisition=acquisition)
            for j in range(2):
                parent, middle, child = feed(f"p{i}-{j}"), feed(f"m{i}-{j}"), feed(f"c{i}-{j}")
                middle.parents.add(parent)
                child.parents.add(middle)
                middle.entries.add(entry)

            ShelfRecord.objects.create(user=cls.user, entry=entry)
            UserActivity.objects.create(user=cls.user, entry=entry, action=UserActivity.ActivityAction.SHELF_ADDED)
            License.objects.create(
                entry=entry,
                user=cls.user,
                state=License.LicenseState.ACTIVE,
                lcp_license_id=uuid.uuid4(),
                starts_at=timezone.now(),
                expires_at=timezone.now() + datetime.timedelta(days=14),
            )
            Reservation.objects.create(entry=entry, user=cls.user, position=1)
            cls.entries.append(entry)

    def setUp(self):
        key = ApiKey.objects.create(user=self.user, name="test")
        self.headers = {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def _count(self, url: str, limit: int) -> int:
        separator = "&" if "?" in url else "?"
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"{url}{separator}limit={limit}", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(len(response.json()["items"]), limit, url)
        return len(queries)

    measured = {}

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        # The fixed per-request cost, printed so a run documents it (`--verbosity 2` keeps it readable).
        for url, count in sorted(cls.measured.items()):
            print(f"queries per page: {count:3d}  {url.split('?')[0]}")

    def assertConstantQueries(self, url: str):
        # Warm-up: the first request of a test pays one-off costs (content types, sessions).
        self._count(url, PAGE)
        small, large = self._count(url, PAGE), self._count(url, 2 * PAGE)
        self.assertEqual(small, large, f"{url}: {small} queries for {PAGE} rows, {large} for {2 * PAGE}")
        self.measured[url] = small

    def test_entries(self):
        self.assertConstantQueries(f"/api/v1/entries?catalog_id={self.catalog.pk}")

    def test_entries_serialize_feed_relations(self):
        response = self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}&limit=1", headers=self.headers)
        feeds = response.json()["items"][0]["feeds"]
        self.assertEqual(len(feeds), 2)
        for feed in feeds:
            self.assertEqual(len(feed["parents"]), 1)
            self.assertEqual(len(feed["children"]), 1)
            self.assertIn("/main/", feed["url"])

    def test_feeds(self):
        self.assertConstantQueries("/api/v1/feeds")

    def test_activity(self):
        self.assertConstantQueries("/api/v1/activity")

    def test_shelf_records(self):
        self.assertConstantQueries("/api/v1/shelf-records")

    def test_licenses(self):
        self.assertConstantQueries(f"/readium/v1/licenses?user_id={self.user.pk}")

    def test_reservations(self):
        self.assertConstantQueries("/readium/v1/reservations")

    def test_user_acquisitions(self):
        self.assertConstantQueries("/api/v1/user-acquisitions")
