"""#215: `UserAcquisition.expire_at` ends the grant on every read path, and reuse never hands it back."""

import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User, UserAcquisition
from apps.files import signing
from apps.files.storage import get_storage

MOBI = b"BOOKMOBI" + bytes(range(256)) * 8
EXPIRED = "/user-acquisition-expired"


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class ExpiredGrantTests(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)
        # The field built its storage at import (the real data directory); use this test's root.
        patcher = mock.patch.object(Acquisition._meta.get_field("content"), "storage", get_storage())
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()

        self.source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        self.reader = self._user("reader", is_superuser=True)
        catalog = Catalog.objects.create(creator=self.reader, title="Grants", url_name="grants", is_public=True)
        self.entry = Entry.objects.create(creator=self.reader, catalog=catalog, title="Expiring Book")
        # MOBI is served as stored: no personalised render in these tests.
        self.acquisition = Acquisition.objects.create(entry=self.entry, mime=Acquisition.AcquisitionMIME.MOBI)
        self.acquisition.content.save("book.mobi", ContentFile(MOBI), save=True)

    def _user(self, username, **extra) -> User:
        user = User(username=username, name="Re", surname="Ader", auth_source=self.source, **extra)
        user.set_unusable_password()
        user.save()
        return user

    def _headers(self, user=None) -> dict:
        user = user or self.reader
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def _grant(self, expire_at=None, type_=UserAcquisition.UserAcquisitionType.PERSONAL) -> UserAcquisition:
        return UserAcquisition.objects.create(
            acquisition=self.acquisition, user=self.reader, type=type_, expire_at=expire_at
        )

    def _download_url(self, grant) -> str:
        return reverse("files:user-acquisition-download", kwargs={"user_acquisition_id": grant.pk})

    def _past(self):
        return timezone.now() - timedelta(minutes=1)

    def assertExpired(self, response):
        self.assertEqual(response.status_code, 410, getattr(response, "content", b"")[:300])
        self.assertEqual(response.json()["type"], EXPIRED)

    # --- the model ----------------------------------------------------------------------------------------

    def test_is_expired_and_live(self):
        live = self._grant()
        later = self._grant(expire_at=timezone.now() + timedelta(days=1))
        past = self._grant(expire_at=self._past())
        self.assertFalse(live.is_expired)
        self.assertFalse(later.is_expired)
        self.assertTrue(past.is_expired)
        self.assertEqual(set(UserAcquisition.objects.live()), {live, later})

    # --- download ---------------------------------------------------------------------------------------------

    def test_a_live_grant_downloads(self):
        grant = self._grant(expire_at=timezone.now() + timedelta(days=1))
        response = self.client.get(self._download_url(grant), headers=self._headers())
        self.assertEqual(response.status_code, 200)

    def test_an_expired_personal_grant_is_gone_whole_or_ranged(self):
        grant = self._grant(expire_at=self._past())
        headers = self._headers()
        self.assertExpired(self.client.get(self._download_url(grant), headers=headers))
        self.assertExpired(self.client.get(self._download_url(grant), headers={**headers, "Range": "bytes=0-9"}))
        self.assertEqual(self.client.head(self._download_url(grant), headers=headers).status_code, 410)

    def test_an_expired_shared_grant_is_gone_for_anyone(self):
        grant = self._grant(expire_at=self._past(), type_=UserAcquisition.UserAcquisitionType.SHARED)
        self.assertExpired(self.client.get(self._download_url(grant)))

    def test_nothing_is_counted_for_an_expired_grant(self):
        grant = self._grant(expire_at=self._past())
        self.client.get(self._download_url(grant), headers=self._headers())
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.popularity, 0)

    def test_a_session_pinned_before_expiry_ends_with_it(self):
        grant = self._grant(expire_at=timezone.now() + timedelta(days=1))
        headers = self._headers()
        self.assertEqual(self.client.get(self._download_url(grant), headers=headers).status_code, 200)
        self.assertEqual(
            self.client.get(self._download_url(grant), headers={**headers, "Range": "bytes=0-9"}).status_code, 206
        )
        UserAcquisition.objects.filter(pk=grant.pk).update(expire_at=self._past())
        self.assertExpired(self.client.get(self._download_url(grant), headers={**headers, "Range": "bytes=10-19"}))

    def test_a_signed_url_signed_before_expiry_ends_with_the_grant(self):
        grant = self._grant(expire_at=timezone.now() + timedelta(days=1))
        query, _expires = signing.signed_query(grant)
        UserAcquisition.objects.filter(pk=grant.pk).update(expire_at=self._past())
        self.assertExpired(self.client.get(f"{self._download_url(grant)}?{query}"))

    # --- the grant's own record and the checker ---------------------------------------------------------------

    def test_the_holder_still_sees_the_record_of_an_expired_grant(self):
        grant = self._grant(expire_at=self._past())
        response = self.client.get(
            reverse("api:user-acquisition-detail", kwargs={"user_acquisition_id": grant.pk}), headers=self._headers()
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["response"]["expire_at"])

    def test_annotations_cannot_be_made_on_an_expired_grant(self):
        # A plain reader: object checks let a superuser through by design.
        member = self._user("member")
        live = UserAcquisition.objects.create(acquisition=self.acquisition, user=member)
        expired = UserAcquisition.objects.create(acquisition=self.acquisition, user=member, expire_at=self._past())

        def annotate(grant):
            return self.client.post(
                reverse("api:annotation-management"),
                data={"user_acquisition_id": str(grant.pk), "title": "Notes"},
                content_type="application/json",
                headers=self._headers(member),
            )

        self.assertEqual(annotate(live).status_code, 201)
        self.assertEqual(annotate(expired).status_code, 403)

    # --- reuse in `single` mode -----------------------------------------------------------------------------

    def test_post_never_hands_back_an_expired_grant(self):
        expired = self._grant(expire_at=self._past())
        response = self.client.post(
            reverse("api:user-acquisition-management"),
            data={"acquisition_id": str(self.acquisition.pk), "type": "personal"},
            content_type="application/json",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 201, response.content[:300])
        self.assertNotEqual(response.json()["response"]["id"], str(expired.pk))
        self.assertTrue(UserAcquisition.objects.filter(pk=expired.pk).exists())  # kept as history

    def test_post_still_hands_back_a_live_grant(self):
        live = self._grant(expire_at=timezone.now() + timedelta(days=1))
        response = self.client.post(
            reverse("api:user-acquisition-management"),
            data={"acquisition_id": str(self.acquisition.pk), "type": "personal"},
            content_type="application/json",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response["Location"].endswith(str(live.pk)))

    @override_settings(EVILFLOWERS_ENFORCE_USER_ACQUISITIONS=True)
    def test_acquisition_download_redirects_past_an_expired_grant(self):
        expired = self._grant(expire_at=self._past())
        response = self.client.get(
            reverse("files:acquisition-download", kwargs={"acquisition_id": self.acquisition.pk}),
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(str(expired.pk), response["Location"])
        followed = self.client.get(response["Location"], headers=self._headers())
        self.assertEqual(followed.status_code, 200)
