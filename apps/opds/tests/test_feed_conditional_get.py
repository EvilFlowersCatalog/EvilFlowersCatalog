"""
Issue #72: OPDS 1.2 feeds answer re-polls with a pre-render `304`, and large
feeds are paged.

An OPDS feed is per user (a `.lcpl` link with a freshly minted capability
token for every title the user holds a license on), so these tests pin down
the ways a validator could go stale or leak:

- a borrow or return must change the next answer, though it touches no entry;
- an ETag must roll over before the tokens in a revalidated copy expire;
- two users never share a validator;
- the `304` path must not render the feed at all.

All requests go through `self.client`, so `GZipMiddleware` (which weakens the
ETag to `W/"..."`) and `ConditionalGetMiddleware` are part of the test.
"""

import datetime
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Entry, Feed, ShelfRecord, User
from apps.opds.schema import Link, LinkType
from apps.readium.models import License

COMPLETE = "/opds/v1.2/open/complete"
RENDER = "apps.opds.services.feeds.AcquisitionEntry.from_model"


class FeedConditionalGetTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.owner = cls._user(source, "owner")
        cls.reader = cls._user(source, "reader")
        cls.other = cls._user(source, "other")

        cls.catalog = Catalog.objects.create(creator=cls.owner, title="Open", url_name="open", is_public=True)
        cls.plain = Entry.objects.create(creator=cls.owner, catalog=cls.catalog, title="Plain book")
        cls.protected = Entry.objects.create(
            creator=cls.owner,
            catalog=cls.catalog,
            title="Protected book",
            config={"readium_enabled": True},
        )
        cls.license = License.objects.create(
            entry=cls.protected,
            user=cls.reader,
            state=License.LicenseState.READY,
            starts_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(days=14),
        )

    @staticmethod
    def _user(source, username):
        user = User(username=username, name=username.title(), surname="Test", auth_source=source)
        user.set_unusable_password()
        user.save()
        return user

    @staticmethod
    def auth(user) -> dict:
        api_key = ApiKey.objects.create(user=user, name=f"opds-{user.username}")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(api_key.pk))}"}

    def revalidate(self, url, etag, headers=None):
        return self.client.get(url, headers={**(headers or {}), "If-None-Match": etag})


class NotModifiedTests(FeedConditionalGetTestCase):
    def test_a_matching_etag_is_answered_304_without_rendering(self):
        first = self.client.get(COMPLETE)
        self.assertEqual(first.status_code, 200)
        self.assertIn("ETag", first.headers)

        with mock.patch(RENDER, side_effect=AssertionError("feed rendered on a 304")):
            second = self.revalidate(COMPLETE, first.headers["ETag"])

        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")

    def test_the_weak_etag_of_a_gzipped_response_revalidates(self):
        headers = {"Accept-Encoding": "gzip"}
        first = self.client.get(COMPLETE, headers=headers)
        self.assertEqual(first.headers.get("Content-Encoding"), "gzip")
        self.assertTrue(first.headers["ETag"].startswith('W/"'))

        self.assertEqual(self.revalidate(COMPLETE, first.headers["ETag"], headers).status_code, 304)

    def test_editing_an_entry_invalidates_the_feed(self):
        etag = self.client.get(COMPLETE).headers["ETag"]

        self.plain.title = "Plain book, 2nd edition"
        self.plain.save()

        response = self.revalidate(COMPLETE, etag)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2nd edition", response.content)

    def test_deleting_an_entry_invalidates_the_feed(self):
        etag = self.client.get(COMPLETE).headers["ETag"]
        self.plain.delete()
        self.assertEqual(self.revalidate(COMPLETE, etag).status_code, 200)

    def test_the_response_language_is_part_of_the_validator(self):
        sk = self.client.get(COMPLETE, headers={"Accept-Language": "sk"}).headers["ETag"]
        en = self.client.get(COMPLETE, headers={"Accept-Language": "en"}).headers["ETag"]
        self.assertNotEqual(sk, en)


class PerUserValidatorTests(FeedConditionalGetTestCase):
    def test_users_never_share_a_validator(self):
        anonymous = self.client.get(COMPLETE).headers["ETag"]
        reader = self.client.get(COMPLETE, headers=self.auth(self.reader)).headers["ETag"]
        other = self.client.get(COMPLETE, headers=self.auth(self.other)).headers["ETag"]
        self.assertEqual(len({anonymous, reader, other}), 3)

    def test_returning_a_title_changes_the_next_answer(self):
        headers = self.auth(self.reader)
        first = self.client.get(COMPLETE, headers=headers)
        self.assertIn(str(self.license.pk).encode(), first.content)

        # A bulk update, as sweeps do: no save(), no `updated_at` bump.
        License.objects.filter(pk=self.license.pk).update(state=License.LicenseState.RETURNED)

        second = self.revalidate(COMPLETE, first.headers["ETag"], headers)
        self.assertEqual(second.status_code, 200)
        self.assertNotIn(str(self.license.pk).encode(), second.content)

    def test_borrowing_a_title_changes_the_next_answer(self):
        headers = self.auth(self.other)
        etag = self.client.get(COMPLETE, headers=headers).headers["ETag"]

        loan = License.objects.create(
            entry=self.protected,
            user=self.other,
            state=License.LicenseState.READY,
            starts_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(days=14),
        )

        response = self.revalidate(COMPLETE, etag, headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(loan.pk).encode(), response.content)

    @override_settings(EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS=1800)
    def test_a_license_holder_is_re_rendered_before_feed_tokens_expire(self):
        headers = self.auth(self.reader)
        etag = self.client.get(COMPLETE, headers=headers).headers["ETag"]

        later = timezone.now() + datetime.timedelta(seconds=1800)
        with mock.patch("apps.opds.services.conditional.timezone.now", return_value=later):
            self.assertEqual(self.revalidate(COMPLETE, etag, headers).status_code, 200)

    def test_without_licenses_there_is_no_time_bucket(self):
        headers = self.auth(self.other)
        etag = self.client.get(COMPLETE, headers=headers).headers["ETag"]

        later = timezone.now() + datetime.timedelta(days=1)
        with mock.patch("apps.opds.services.conditional.timezone.now", return_value=later):
            self.assertEqual(self.revalidate(COMPLETE, etag, headers).status_code, 304)


class CacheHeaderTests(FeedConditionalGetTestCase):
    def test_an_authenticated_feed_is_private_and_varies_on_the_credential(self):
        response = self.client.get(COMPLETE, headers=self.auth(self.reader))
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("Authorization", response.headers["Vary"])
        self.assertIn("Accept-Language", response.headers["Vary"])

    def test_an_anonymous_feed_must_be_revalidated(self):
        response = self.client.get(COMPLETE)
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertNotIn("private", response.headers["Cache-Control"])

    def test_a_304_carries_the_same_headers(self):
        headers = self.auth(self.reader)
        etag = self.client.get(COMPLETE, headers=headers).headers["ETag"]
        response = self.revalidate(COMPLETE, etag, headers)
        self.assertEqual(response.status_code, 304)
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("Authorization", response.headers["Vary"])


class CompleteFeedPagingTests(FeedConditionalGetTestCase):
    def test_a_small_catalog_is_served_whole_and_marked_complete(self):
        content = self.client.get(COMPLETE).content
        self.assertIn(b"<fh:complete", content)
        self.assertNotIn(b'rel="next"', content)
        self.assertEqual(content.count(b"<entry>"), 2)

    @override_settings(EVILFLOWERS_OPDS_COMPLETE_FEED_MAX_ENTRIES=1)
    def test_an_extremely_large_catalog_is_paged_without_the_complete_marker(self):
        first = self.client.get(COMPLETE)
        self.assertNotIn(b"<fh:complete", first.content)
        self.assertEqual(first.content.count(b"<entry>"), 1)
        self.assertIn(b'rel="next" href="/opds/v1.2/open/complete?page=2"', first.content)

        second = self.client.get(f"{COMPLETE}?page=2")
        self.assertEqual(second.status_code, 200)
        self.assertIn(b'rel="previous"', second.content)
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    @override_settings(EVILFLOWERS_OPDS_COMPLETE_FEED_MAX_ENTRIES=1)
    def test_paging_keeps_a_query_string_credential(self):
        api_key = ApiKey.objects.create(user=self.reader, name="query")
        token = JWTFactory(self.reader.pk).api_key(str(api_key.pk))
        content = self.client.get(f"{COMPLETE}?access_token={token}").content
        self.assertIn(f"access_token={token}&amp;page=2".encode(), content)

    def test_a_page_past_the_end_is_404(self):
        self.assertEqual(self.client.get(f"{COMPLETE}?page=99").status_code, 404)

    def test_a_malformed_page_is_400(self):
        self.assertEqual(self.client.get(f"{COMPLETE}?page=abc").status_code, 400)

    def test_entries_are_ordered_by_atom_updated_newest_first(self):
        self.plain.save()  # now the most recently updated
        content = self.client.get(COMPLETE).content
        self.assertLess(content.index(b"Plain book"), content.index(b"Protected book"))


class ShelfFeedTests(FeedConditionalGetTestCase):
    def test_the_shelf_lists_only_this_catalogs_entries(self):
        elsewhere = Catalog.objects.create(creator=self.owner, title="Other", url_name="other", is_public=True)
        stray = Entry.objects.create(creator=self.owner, catalog=elsewhere, title="Book from elsewhere")
        ShelfRecord.objects.create(entry=self.plain, user=self.reader)
        ShelfRecord.objects.create(entry=stray, user=self.reader)

        content = self.client.get("/opds/v1.2/open/shelf", headers=self.auth(self.reader)).content
        self.assertIn(b"Plain book", content)
        self.assertNotIn(b"Book from elsewhere", content)

    def test_shelving_a_title_changes_the_next_answer(self):
        headers = self.auth(self.reader)
        etag = self.client.get("/opds/v1.2/open/shelf", headers=headers).headers["ETag"]
        ShelfRecord.objects.create(entry=self.plain, user=self.reader)
        self.assertEqual(self.revalidate("/opds/v1.2/open/shelf", etag, headers).status_code, 200)


class CuratedFeedTests(FeedConditionalGetTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.shelf = Feed.objects.create(
            catalog=cls.catalog,
            creator=cls.owner,
            title={"en": "Reading list"},
            url_name="reading-list",
            kind=Feed.FeedKind.ACQUISITION,
            content="",
            per_page=1,
        )
        cls.shelf.entries.add(cls.plain, cls.protected)
        cls.menu = Feed.objects.create(
            catalog=cls.catalog,
            creator=cls.owner,
            title={"en": "Browse"},
            url_name="browse",
            kind=Feed.FeedKind.NAVIGATION,
            content="",
        )
        cls.shelf.parents.add(cls.menu)

    def test_an_acquisition_feed_is_paged_at_its_own_page_size(self):
        url = "/opds/v1.2/open/feed/reading-list"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content.count(b"<entry>"), 1)
        self.assertIn(b'rel="next" href="/opds/v1.2/open/feed/reading-list?page=2"', first.content)
        self.assertIn(b'rel="related" href="/opds/v1.2/open/feed/browse"', first.content)

        second = self.client.get(f"{url}?page=2")
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])
        self.assertEqual(self.revalidate(url, first.headers["ETag"]).status_code, 304)

    def test_removing_an_entry_from_the_feed_invalidates_it(self):
        url = "/opds/v1.2/open/feed/reading-list"
        etag = self.client.get(url).headers["ETag"]
        self.shelf.entries.remove(self.protected)
        self.assertEqual(self.revalidate(url, etag).status_code, 200)

    def test_a_navigation_feed_revalidates_until_a_child_changes(self):
        url = "/opds/v1.2/open/feed/browse"
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertIn(b"Reading list", first.content)
        self.assertEqual(self.revalidate(url, first.headers["ETag"]).status_code, 304)

        self.shelf.title = {"en": "Summer reading"}
        self.shelf.save()

        second = self.revalidate(url, first.headers["ETag"], {"Accept-Language": "en"})
        self.assertEqual(second.status_code, 200)
        self.assertIn(b"Summer reading", second.content)


class TopEntriesFeedTests(FeedConditionalGetTestCase):
    def test_the_latest_feed_revalidates(self):
        etag = self.client.get("/opds/v1.2/open/new").headers["ETag"]
        self.assertEqual(self.revalidate("/opds/v1.2/open/new", etag).status_code, 304)

    def test_the_latest_feed_has_a_stable_atom_id(self):
        first = self.client.get("/opds/v1.2/open/new").content
        second = self.client.get("/opds/v1.2/open/new").content
        self.assertEqual(first.split(b"<id>")[1].split(b"</id>")[0], second.split(b"<id>")[1].split(b"</id>")[0])


class PagingLinkSchemaTests(SimpleTestCase):
    """Same closed-enum trap as `test_borrow_link_schema`: a missing member 500s every paged feed."""

    def test_the_rfc5005_relations_are_valid_link_types(self):
        for rel in ("first", "previous", "next", "last"):
            self.assertEqual(Link(rel=rel, href="/x", type="application/atom+xml").rel, LinkType(rel))
