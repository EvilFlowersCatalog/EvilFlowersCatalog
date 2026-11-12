"""Feed uniqueness over REST.

Only `(catalog, url_name)` is unique. A repeated title used to reach the database
and answer 500; a repeated slug answered a bare 409 the portal could not act on.
The 409 now names the feed holding the slug and suggests a free one.
"""

import json

from django.test import TestCase

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Feed, User


class FeedUniquenessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.root = User(username="root", name="Root", surname="T", auth_source=source, is_superuser=True)
        cls.root.set_unusable_password()
        cls.root.save()
        cls.catalog = Catalog.objects.create(creator=cls.root, title="Main", url_name="main")
        cls.other_catalog = Catalog.objects.create(creator=cls.root, title="Other", url_name="other")

    def setUp(self):
        key = ApiKey.objects.create(user=self.root, name="test")
        self.headers = {"Authorization": f"Bearer {JWTFactory(self.root.pk).api_key(str(key.pk))}"}

    def _payload(self, **overrides) -> dict:
        return {
            "catalog_id": str(self.catalog.pk),
            "title": {"sk": "Novinky"},
            "url_name": "novinky",
            "kind": "navigation",
            "content": "x",
            **overrides,
        }

    def _post(self, payload):
        return self.client.post(
            "/api/v1/feeds", data=json.dumps(payload), content_type="application/json", headers=self.headers
        )

    def _put(self, feed_id, payload):
        return self.client.put(
            f"/api/v1/feeds/{feed_id}", data=json.dumps(payload), content_type="application/json", headers=self.headers
        )

    def test_same_title_different_url_name_is_created(self):
        self.assertEqual(self._post(self._payload()).status_code, 201)
        self.assertEqual(self._post(self._payload(url_name="novinky-fiit")).status_code, 201)
        self.assertEqual(Feed.objects.filter(catalog=self.catalog).count(), 2)

    def test_same_url_name_is_a_conflict_with_a_suggestion(self):
        first = self._post(self._payload()).json()["response"]
        self._post(self._payload(url_name="novinky-2"))

        response = self._post(self._payload(title={"sk": "Iné"}))

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["type"], "/feed-url-name-taken")
        data = body["additional_data"]
        self.assertEqual(data["field"], "url_name")
        self.assertEqual(data["suggested_url_name"], "novinky-3")
        self.assertEqual(data["conflicting_feed"]["id"], first["id"])

    def test_same_url_name_in_another_catalog_is_fine(self):
        self.assertEqual(self._post(self._payload()).status_code, 201)
        self.assertEqual(self._post(self._payload(catalog_id=str(self.other_catalog.pk))).status_code, 201)

    def test_update_keeping_own_url_name_is_not_a_conflict(self):
        feed_id = self._post(self._payload()).json()["response"]["id"]
        self.assertEqual(self._put(feed_id, self._payload(content="changed")).status_code, 200)

    def test_update_to_a_taken_url_name_is_a_conflict(self):
        self._post(self._payload())
        feed_id = self._post(self._payload(url_name="ine")).json()["response"]["id"]
        response = self._put(feed_id, self._payload())
        self.assertEqual(response.status_code, 409)
        # The feed being edited does not count against the suggestion.
        self.assertEqual(response.json()["additional_data"]["suggested_url_name"], "novinky-2")

    def test_url_name_filter_reports_availability(self):
        self._post(self._payload())
        taken = self.client.get(
            "/api/v1/feeds", {"catalog_id": str(self.catalog.pk), "url_name": "novinky"}, headers=self.headers
        )
        free = self.client.get(
            "/api/v1/feeds", {"catalog_id": str(self.catalog.pk), "url_name": "volne"}, headers=self.headers
        )
        self.assertEqual(len(taken.json()["items"]), 1)
        self.assertEqual(len(free.json()["items"]), 0)


class AvailableUrlNameTests(TestCase):
    def test_long_slugs_stay_within_max_length(self):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        user = User.objects.create(username="u", name="U", surname="T", auth_source=source)
        catalog = Catalog.objects.create(creator=user, title="C", url_name="c")
        slug = "a" * 50
        Feed.objects.create(catalog=catalog, creator=user, title={"sk": "x"}, url_name=slug, kind="navigation")

        suggestion = Feed.available_url_name(catalog, slug)

        self.assertEqual(suggestion, "a" * 48 + "-2")
