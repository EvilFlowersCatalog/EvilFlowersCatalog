"""Regression (#60): `GET /api/v1/entries?query=` keeps the relevance ranking.

`EntryFilter.filter_query` orders by `-relevance_score`, but `PaginationResponse`
used to re-apply the default `order_by=created_at` and throw that ranking away.
"""

from django.test import TestCase

from apps.core.models import AuthSource, Catalog, Entry, User


class EntryListRelevanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        owner = User(username="owner", name="O", surname="W", auth_source=source)
        owner.set_unusable_password()
        owner.save()
        cls.catalog = Catalog.objects.create(creator=owner, title="Open", url_name="open", is_public=True)
        # Older entry matches only in the summary, newer one in the title.
        cls.summary_match = Entry.objects.create(
            creator=owner, catalog=cls.catalog, title="Zebras", summary="A book about algebra."
        )
        cls.title_match = Entry.objects.create(creator=owner, catalog=cls.catalog, title="Algebra")

    def _ids(self, params):
        response = self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}&{params}")
        self.assertEqual(response.status_code, 200, response.content[:300])
        return [item["id"] for item in response.json()["items"]]

    def test_query_orders_by_relevance(self):
        self.assertEqual(self._ids("query=algebra"), [str(self.title_match.pk), str(self.summary_match.pk)])

    def test_explicit_order_by_still_wins(self):
        self.assertEqual(
            self._ids("query=algebra&order_by=created_at"),
            [str(self.summary_match.pk), str(self.title_match.pk)],
        )
