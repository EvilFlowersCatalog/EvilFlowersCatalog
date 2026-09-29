"""Regression: an anonymous `GET /api/v1/entries` must not 500.

Seen on production 2026-09-29: the portal appended `?access_token=` to a URL
that already had a query string, so the token was never parsed, the request ran
as `AnonymousUser`, and `shelf_record_mapping` raised
`ValidationError: “AnonymousUser” is not a valid UUID`.
"""

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from apps.api.views.entries import shelf_record_mapping
from apps.core.models import AuthSource, Catalog, Entry, User


class AnonymousEntryListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        owner = User(username="owner", name="O", surname="W", auth_source=source)
        owner.set_unusable_password()
        owner.save()
        cls.catalog = Catalog.objects.create(creator=owner, title="Open", url_name="open", is_public=True)
        Entry.objects.create(creator=owner, catalog=cls.catalog, title="Public book")

    def test_an_anonymous_user_has_an_empty_shelf(self):
        self.assertEqual(shelf_record_mapping(AnonymousUser()), {})

    def test_a_malformed_access_token_query_is_a_400_not_a_500(self):
        response = self.client.get(
            f"/api/v1/entries?catalog_id={self.catalog.pk}&order_by=-created_at?access_token=not-parsed"
        )
        self.assertEqual(response.status_code, 400, response.content[:300])

    def test_an_anonymous_list_of_a_public_catalog_works(self):
        response = self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}")
        self.assertEqual(response.status_code, 200, response.content[:300])
