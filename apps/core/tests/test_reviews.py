"""IP-023: ratings and reviews of entries."""

import json
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Entry, EntryReview, User, UserActivity, UserCatalog
from apps.core.services.reviews import ReviewService


class ReviewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.owner = cls._user(source, "owner")
        cls.reader = cls._user(source, "reader")
        cls.other = cls._user(source, "other")
        cls.outsider = cls._user(source, "outsider")

        cls.public = Catalog.objects.create(creator=cls.owner, title="Open", url_name="open", is_public=True)
        cls.private = Catalog.objects.create(creator=cls.owner, title="Closed", url_name="closed")
        for catalog in (cls.public, cls.private):
            UserCatalog.objects.create(catalog=catalog, user=cls.owner, mode=UserCatalog.Mode.MANAGE)
        UserCatalog.objects.create(catalog=cls.private, user=cls.reader, mode=UserCatalog.Mode.READ)

        cls.book = Entry.objects.create(creator=cls.owner, catalog=cls.public, title="Open book")
        cls.secret = Entry.objects.create(creator=cls.owner, catalog=cls.private, title="Closed book")

    @staticmethod
    def _user(source, username):
        user = User(username=username, name=username.title(), surname="Test", auth_source=source)
        user.set_unusable_password()
        user.save()
        return user

    @staticmethod
    def auth(user) -> dict:
        api_key = ApiKey.objects.create(user=user, name=f"reviews-{user.username}")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(api_key.pk))}"}

    def put_review(self, user, entry, **payload):
        return self.client.put(
            f"/api/v1/entries/{entry.pk}/reviews/mine",
            data=json.dumps(payload),
            content_type="application/json",
            headers=self.auth(user),
        )

    def moderate(self, user, review, **payload):
        return self.client.patch(
            f"/api/v1/reviews/{review.pk}",
            data=json.dumps(payload),
            content_type="application/json",
            headers=self.auth(user),
        )

    def rating_of(self, entry) -> tuple:
        entry.refresh_from_db()
        return entry.rating_count, entry.rating_sum


class WriteTests(ReviewTestCase):
    def test_create_then_replace(self):
        created = self.put_review(self.reader, self.book, rating=4, title="Good", body="Clear and short.")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["response"]["rating"], 4)
        self.assertEqual(self.rating_of(self.book), (1, 4))

        replaced = self.put_review(self.reader, self.book, rating=2)
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(replaced.json()["response"]["body"], "")
        self.assertEqual(EntryReview.objects.filter(entry=self.book).count(), 1)
        self.assertEqual(self.rating_of(self.book), (1, 2))

        self.assertTrue(
            UserActivity.objects.filter(user=self.reader, action=UserActivity.ActivityAction.ENTRY_REVIEWED).exists()
        )

    def test_rating_is_validated(self):
        for rating in (0, 6, "five", None):
            with self.subTest(rating=rating):
                self.assertEqual(self.put_review(self.reader, self.book, rating=rating).status_code, 422)

    @override_settings(EVILFLOWERS_REVIEWS_BODY_MAX_CHARS=10)
    def test_body_length_is_limited(self):
        self.assertEqual(self.put_review(self.reader, self.book, rating=3, body="x" * 11).status_code, 422)

    def test_anonymous_cannot_review(self):
        response = self.client.put(
            f"/api/v1/entries/{self.book.pk}/reviews/mine", data='{"rating": 5}', content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)

    def test_a_private_entry_is_invisible_to_non_members(self):
        self.assertEqual(self.put_review(self.outsider, self.secret, rating=5).status_code, 404)
        self.assertEqual(self.put_review(self.reader, self.secret, rating=5).status_code, 201)

    def test_delete_own_review(self):
        self.put_review(self.reader, self.book, rating=5)
        self.put_review(self.other, self.book, rating=3)

        response = self.client.delete(f"/api/v1/entries/{self.book.pk}/reviews/mine", headers=self.auth(self.reader))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.rating_of(self.book), (1, 3))

        again = self.client.delete(f"/api/v1/entries/{self.book.pk}/reviews/mine", headers=self.auth(self.reader))
        self.assertEqual(again.status_code, 404)

    def test_deleting_a_user_updates_the_rating(self):
        self.put_review(self.other, self.book, rating=1)
        self.put_review(self.reader, self.book, rating=5)
        self.other.delete()
        self.assertEqual(self.rating_of(self.book), (1, 5))

    @override_settings(EVILFLOWERS_REVIEWS_ENABLED=False)
    def test_disabled(self):
        self.assertEqual(self.put_review(self.reader, self.book, rating=5).status_code, 404)


class ModerationTests(ReviewTestCase):
    def setUp(self):
        self.put_review(self.reader, self.book, rating=1, body="Abusive text")
        self.put_review(self.other, self.book, rating=5)
        self.review = EntryReview.objects.get(entry=self.book, user=self.reader)

    def test_a_manager_hides_a_review(self):
        response = self.moderate(self.owner, self.review, state="hidden", moderation_note="Off-topic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"]["state"], "hidden")
        self.assertEqual(self.rating_of(self.book), (1, 5))

        listed = self.client.get(f"/api/v1/entries/{self.book.pk}/reviews").json()["items"]
        self.assertEqual([item["rating"] for item in listed], [5])

        hidden = self.client.get(
            f"/api/v1/entries/{self.book.pk}/reviews?state=hidden", headers=self.auth(self.owner)
        ).json()["items"]
        self.assertEqual([item["moderation_note"] for item in hidden], ["Off-topic"])

        # The author still sees their own review and that it was hidden.
        mine = self.client.get(f"/api/v1/entries/{self.book.pk}/reviews/mine", headers=self.auth(self.reader))
        self.assertEqual(mine.json()["response"]["state"], "hidden")

    def test_editing_a_hidden_review_keeps_it_hidden(self):
        self.moderate(self.owner, self.review, state="hidden")
        self.put_review(self.reader, self.book, rating=4, body="Rewritten")
        self.review.refresh_from_db()
        self.assertEqual(self.review.state, "hidden")
        self.assertEqual(self.rating_of(self.book), (1, 5))

    def test_readers_cannot_moderate(self):
        self.assertEqual(self.moderate(self.other, self.review, state="hidden").status_code, 403)
        self.assertEqual(self.moderate(self.reader, self.review, state="hidden").status_code, 403)

    def test_non_moderators_do_not_see_hidden_reviews(self):
        self.moderate(self.owner, self.review, state="hidden")
        listed = self.client.get(
            f"/api/v1/entries/{self.book.pk}/reviews?state=hidden", headers=self.auth(self.other)
        ).json()["items"]
        self.assertEqual([item["rating"] for item in listed], [5])
        self.assertNotIn("moderation_note", listed[0])

    def test_invalid_state(self):
        self.assertEqual(self.moderate(self.owner, self.review, state="deleted").status_code, 422)


class ReviewListTests(ReviewTestCase):
    """`GET /api/v1/reviews`: every review a moderator may moderate, across entries."""

    def setUp(self):
        self.put_review(self.reader, self.book, rating=1, body="Abusive text")
        self.put_review(self.reader, self.secret, rating=4, title="Closed but good")
        self.put_review(self.other, self.book, rating=5)
        self.hidden = EntryReview.objects.get(entry=self.book, user=self.reader)
        self.moderate(self.owner, self.hidden, state="hidden", moderation_note="Off-topic")

    def list(self, user, query=""):
        return self.client.get(f"/api/v1/reviews{query}", headers=self.auth(user))

    def test_a_manager_sees_reviews_of_managed_catalogs(self):
        response = self.list(self.owner)
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual({item["entry"]["title"] for item in items}, {"Open book", "Closed book"})
        self.assertIn("moderation_note", items[0])

    def test_a_manager_of_one_catalog_sees_only_that_catalog(self):
        UserCatalog.objects.create(catalog=self.private, user=self.other, mode=UserCatalog.Mode.MANAGE)
        items = self.list(self.other).json()["items"]
        self.assertEqual([item["entry"]["id"] for item in items], [str(self.secret.pk)])

    def test_filters_and_ordering(self):
        hidden = self.list(self.owner, "?state=hidden").json()["items"]
        self.assertEqual([item["moderation_note"] for item in hidden], ["Off-topic"])

        by_entry = self.list(self.owner, f"?entry_id={self.secret.pk}").json()["items"]
        self.assertEqual([item["rating"] for item in by_entry], [4])

        found = self.list(self.owner, "?query=closed").json()["items"]
        self.assertEqual([item["entry"]["id"] for item in found], [str(self.secret.pk)])

        best_first = self.list(self.owner, "?order_by=-rating").json()["items"]
        self.assertEqual([item["rating"] for item in best_first], [5, 4, 1])

        self.assertEqual(self.list(self.owner, "?order_by=title").status_code, 400)

    def test_readers_and_anonymous_are_refused(self):
        self.assertEqual(self.list(self.reader).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/reviews").status_code, 401)


class EntryRatingTests(ReviewTestCase):
    def test_rating_block_on_list_and_detail(self):
        self.put_review(self.reader, self.book, rating=4)
        self.put_review(self.other, self.book, rating=5)

        items = self.client.get(f"/api/v1/entries?id={self.book.pk}", headers=self.auth(self.reader)).json()
        self.assertEqual(items["items"][0]["rating"], {"average": 4.5, "count": 2, "mine": 4})

        anonymous = self.client.get(f"/api/v1/entries?id={self.book.pk}").json()
        self.assertEqual(anonymous["items"][0]["rating"], {"average": 4.5, "count": 2, "mine": None})

        detail = self.client.get(
            f"/api/v1/catalogs/{self.public.pk}/entries/{self.book.pk}", headers=self.auth(self.owner)
        ).json()
        self.assertEqual(detail["response"]["rating"]["count"], 2)

    def test_unrated_entry(self):
        items = self.client.get(f"/api/v1/entries?id={self.book.pk}").json()
        self.assertEqual(items["items"][0]["rating"], {"average": None, "count": 0, "mine": None})

    def test_order_by_rating_uses_the_bayesian_average(self):
        single = Entry.objects.create(creator=self.owner, catalog=self.public, title="One five-star rating")
        many = Entry.objects.create(creator=self.owner, catalog=self.public, title="Many 4.8 ratings")
        # 1 × 5 vs 40 × 4.8 (sum 192), written straight to the aggregate.
        Entry.objects.filter(pk=single.pk).update(rating_count=1, rating_sum=5)
        Entry.objects.filter(pk=many.pk).update(rating_count=40, rating_sum=192)

        response = self.client.get(f"/api/v1/entries?catalog_id={self.public.pk}&order_by=-rating")
        titles = [item["title"] for item in response.json()["items"]]
        self.assertLess(titles.index("Many 4.8 ratings"), titles.index("One five-star rating"))

    def test_rating_min(self):
        self.put_review(self.reader, self.book, rating=3)
        good = Entry.objects.create(creator=self.owner, catalog=self.public, title="Good")
        self.put_review(self.reader, good, rating=5)

        response = self.client.get(f"/api/v1/entries?catalog_id={self.public.pk}&rating_min=4")
        self.assertEqual([item["title"] for item in response.json()["items"]], ["Good"])

    def test_a_stale_entry_save_keeps_the_rating(self):
        stale = Entry.objects.get(pk=self.book.pk)
        self.put_review(self.reader, self.book, rating=5)

        stale.popularity += 1
        stale.save()

        self.assertEqual(self.rating_of(self.book), (1, 5))
        self.book.refresh_from_db()
        self.assertEqual(self.book.popularity, 1)

    def test_list_ordering_and_bad_order_by(self):
        self.put_review(self.reader, self.book, rating=2)
        self.put_review(self.other, self.book, rating=5)
        listed = self.client.get(f"/api/v1/entries/{self.book.pk}/reviews?order_by=-rating").json()["items"]
        ratings = [item["rating"] for item in listed]
        self.assertEqual(ratings, [5, 2])
        self.assertEqual(self.client.get(f"/api/v1/entries/{self.book.pk}/reviews?order_by=title").status_code, 400)


class RecomputeCommandTests(ReviewTestCase):
    def test_check_and_rebuild(self):
        ReviewService.upsert(self.reader, self.book, rating=4)
        Entry.objects.filter(pk=self.book.pk).update(rating_count=7, rating_sum=1)

        with self.assertRaises(CommandError):
            call_command("recompute_ratings", "--check", stdout=StringIO())

        call_command("recompute_ratings", stdout=StringIO())
        self.assertEqual(self.rating_of(self.book), (1, 4))
        call_command("recompute_ratings", "--check", stdout=StringIO())
