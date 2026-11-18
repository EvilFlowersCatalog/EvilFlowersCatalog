"""IP-036: equivalent grants, reader-scoped copies, the database-free range path and its revocation, DELETE, pre-render."""

import shutil
import tempfile
import time
from datetime import timedelta
from unittest import mock

import fitz
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import (
    Acquisition,
    Annotation,
    AnnotationItem,
    ApiKey,
    AuthSource,
    Catalog,
    Entry,
    User,
    UserAcquisition,
)
from apps.core.modifiers.pdf import PDFModifier
from apps.files import copies, personalised, revocation, signing, views
from apps.files.storage import get_storage

PDF_MODIFIERS = {"application/pdf": "apps.core.modifiers.pdf.PDFModifier"}


def _pdf(pages=3) -> bytes:
    document = fitz.open()
    for number in range(pages):
        document.new_page().insert_text((72, 72), f"Page {number + 1}")
    return document.tobytes()


def _body(response) -> bytes:
    return b"".join(response.streaming_content)


@override_settings(
    EVILFLOWERS_EVENT_BROKER_EXECUTOR=None,
    EVILFLOWERS_MODIFIERS=PDF_MODIFIERS,
    EVILFLOWERS_PERSONALISED_COPY_SCOPE="reader",
    EVILFLOWERS_FILES_RANGE_FAST_PATH=True,
)
class ReaderGrantTests(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)
        mock.patch.object(Acquisition._meta.get_field("content"), "storage", get_storage()).start()
        # No broker: no shared base, no pre-render unless a test runs the job itself.
        mock.patch.object(personalised, "queue_base").start()
        self.queue_prerender = mock.patch.object(copies, "queue_prerender").start()
        self.addCleanup(mock.patch.stopall)
        cache.clear()

        self.source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        self.manager = self._user("manager", is_superuser=True)
        self.reader = self._user("reader")
        self.other = self._user("other")
        self.catalog = Catalog.objects.create(creator=self.manager, title="Grants", url_name="grants", is_public=True)
        self.catalog.users.add(self.reader, self.other)
        self.entry = Entry.objects.create(
            creator=self.manager,
            catalog=self.catalog,
            title="Streamed Book",
            config={"evilflowers_share_enabled": True},
        )
        self.acquisition = Acquisition.objects.create(entry=self.entry, mime=Acquisition.AcquisitionMIME.PDF)
        self.acquisition.content.save("book.pdf", ContentFile(_pdf()), save=True)

    # --- helpers ---------------------------------------------------------------------------------------------

    def _user(self, username, **extra) -> User:
        user = User(username=username, name=username.title(), surname="User", auth_source=self.source, **extra)
        user.set_unusable_password()
        user.save()
        return user

    def _headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def _grant(self, user=None, **extra) -> UserAcquisition:
        return UserAcquisition.objects.create(acquisition=self.acquisition, user=user or self.reader, **extra)

    def _url(self, grant, query="") -> str:
        url = reverse("files:user-acquisition-download", kwargs={"user_acquisition_id": grant.pk})
        return f"{url}?{query}" if query else url

    def _signed(self, grant) -> str:
        query, _ = signing.signed_query(grant)
        return self._url(grant, query)

    def _post(self, user, **data):
        return self.client.post(
            reverse("api:user-acquisition-management"),
            data={"acquisition_id": str(self.acquisition.pk), "type": "personal", **data},
            content_type="application/json",
            headers=self._headers(user),
        )

    def _licence_text(self, body: bytes) -> str:
        return fitz.open(stream=body)[1].get_text()

    # --- A: equivalent grants -----------------------------------------------------------------------------

    def test_an_equivalent_live_grant_is_reused(self):
        created = self._post(self.reader)
        self.assertEqual(created.status_code, 201, created.content[:300])
        grant_id = created.json()["response"]["id"]
        again = self._post(self.reader)
        self.assertEqual(again.status_code, 303)
        self.assertTrue(again["Location"].endswith(grant_id))

    def test_another_range_is_another_grant(self):
        whole = self._post(self.reader).json()["response"]["id"]
        part = self._post(self.reader, range="1-2")
        self.assertEqual(part.status_code, 201, part.content[:300])
        self.assertNotEqual(part.json()["response"]["id"], whole)
        self.assertEqual(self._post(self.reader, range="1-2").status_code, 303)

    def test_shared_grants_are_always_new(self):
        first = self._post(self.reader, type="shared")
        second = self._post(self.reader, type="shared")
        self.assertEqual((first.status_code, second.status_code), (201, 201))

    @override_settings(EVILFLOWERS_ENFORCE_USER_ACQUISITIONS=True)
    def test_acquisition_download_reuses_the_whole_book_grant(self):
        partial = self._grant(range="1-1")
        url = reverse("files:acquisition-download", kwargs={"acquisition_id": self.acquisition.pk})
        first = self.client.get(url, headers=self._headers(self.reader))
        second = self.client.get(url, headers=self._headers(self.reader))
        self.assertEqual(first["Location"].split("?")[0], second["Location"].split("?")[0])
        self.assertNotIn(str(partial.pk), first["Location"])
        self.assertEqual(UserAcquisition.objects.filter(user=self.reader, range__isnull=True).count(), 1)

    # --- B: reader-scoped copies -------------------------------------------------------------------------------

    def test_a_readers_grants_share_one_copy_watermarked_with_the_copy_id(self):
        one, two = self._grant(), self._grant()
        with mock.patch.object(PDFModifier, "generate", autospec=True, side_effect=PDFModifier.generate) as generate:
            first = _body(self.client.get(self._url(one), headers=self._headers(self.reader)))
            second = _body(self.client.get(self._url(two), headers=self._headers(self.reader)))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(first, second)

        storage = self.acquisition.content.storage
        _folders, files = storage.listdir(f"personalised/{self.reader.pk}")
        self.assertEqual(len(files), 1)
        text = self._licence_text(first)
        self.assertIn(personalised.copy_digest(files[0]), text)
        self.assertIn("reader", text)
        self.assertNotIn(str(one.pk), text)

    @override_settings(EVILFLOWERS_PERSONALISED_COPY_SCOPE="grant")
    def test_grant_scope_keeps_a_copy_per_grant(self):
        one, two = self._grant(), self._grant()
        with mock.patch.object(PDFModifier, "generate", autospec=True, side_effect=PDFModifier.generate) as generate:
            first = _body(self.client.get(self._url(one), headers=self._headers(self.reader)))
            self.client.get(self._url(two), headers=self._headers(self.reader))
        self.assertEqual(generate.call_count, 2)
        self.assertIn(str(one.pk), self._licence_text(first))
        self.assertTrue(self.acquisition.content.storage.exists(f"personalised/{one.pk}"))

    def test_a_shared_grant_is_watermarked_with_whoever_opens_it(self):
        shared = self._grant(type=UserAcquisition.UserAcquisitionType.SHARED)
        own = _body(self.client.get(self._url(shared), headers=self._headers(self.reader)))
        theirs = _body(self.client.get(self._url(shared), headers=self._headers(self.other)))
        self.assertIn("reader", self._licence_text(own))
        self.assertIn("other", self._licence_text(theirs))
        self.assertNotIn("reader (", self._licence_text(theirs))
        # The other reader's range is pinned apart from the owner's.
        part = self.client.get(self._url(shared), headers={**self._headers(self.other), "Range": "bytes=0-9"})
        self.assertEqual(_body(part), theirs[:10])

    def test_annotations_of_the_readers_grants_with_the_same_range_are_drawn(self):
        noted, opened, other_range = self._grant(), self._grant(), self._grant(range="1-2")
        for grant, page in ((noted, 1), (other_range, 1)):
            annotation = Annotation.objects.create(user_acquisition=grant, title="Notes")
            AnnotationItem.objects.create(annotation=annotation, page=page, content=f"<svg id='{grant.pk}'/>")
        copy = copies.prepare(opened, self.reader, annotations=True)
        drawn = [item for items in copy.annotation_map.values() for item in items]
        self.assertEqual(drawn, [f"<svg id='{noted.pk}'/>"])

    def test_the_task_and_the_view_name_the_same_copy(self):
        grant = self._grant()
        self.assertEqual(copies.prepare(grant, grant.user).name, copies.prepare(grant, self.reader).name)

    # --- C: database-free range path -------------------------------------------------------------------------

    def _open(self, grant) -> bytes:
        response = self.client.get(self._signed(grant))
        self.assertEqual(response.status_code, 200, getattr(response, "content", b"")[:300])
        return _body(response)

    def test_a_signed_range_of_a_pinned_session_runs_no_query(self):
        grant = self._grant()
        body = self._open(grant)
        with self.assertNumQueries(0):
            part = self.client.get(self._signed(grant), headers={"Range": "bytes=10-19"})
            self.assertEqual(part.status_code, 206)
            self.assertEqual(_body(part), body[10:20])
        with self.assertNumQueries(0):
            self.assertEqual(self.client.head(self._signed(grant)).status_code, 200)

    def test_a_bearer_range_is_not_served_from_the_fast_path(self):
        grant = self._grant()
        self.client.get(self._url(grant), headers=self._headers(self.reader))
        headers = {**self._headers(self.reader), "Range": "bytes=0-9"}
        # IP-036 Q5: a bearer token is checked by its backend (user, API key) on every range.
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(self._url(grant), headers=headers).status_code, 206)
        self.assertGreater(len(queries), 0)

    def _assert_revoked(self, grant, revoke, status):
        self._open(grant)
        with self.captureOnCommitCallbacks(execute=True):
            revoke()
        response = self.client.get(self._signed(grant), headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, status, getattr(response, "content", b"")[:300])

    def test_revoking_the_grant_refuses_the_next_range(self):
        grant = self._grant()

        def revoke():
            self.client.delete(
                reverse("api:user-acquisition-detail", kwargs={"user_acquisition_id": grant.pk}),
                headers=self._headers(self.reader),
            )

        self._assert_revoked(grant, revoke, 410)

    def test_deactivating_the_reader_refuses_the_next_range(self):
        grant = self._grant()

        def revoke():
            self.reader.is_active = False
            self.reader.save()

        self._assert_revoked(grant, revoke, 403)

    @override_settings(EVILFLOWERS_ALLOWED_IP_RANGES=["10.0.0.0/8"])
    def test_switching_the_ip_block_on_refuses_the_next_range(self):
        grant = self._grant()

        def revoke():
            self.entry.config = {**self.entry.config, "evilflowers_ip_block": True}
            self.entry.save()

        self._assert_revoked(grant, revoke, 403)

    def test_an_expired_grant_is_refused_even_with_a_pin(self):
        grant = self._grant(expire_at=timezone.now() + timedelta(seconds=30))
        self._open(grant)
        # Only the view's clock and the model's move on; the signed URL (30 s left) is still valid.
        later = mock.Mock(time=mock.Mock(return_value=time.time() + 60))
        with (
            mock.patch.object(views, "time", later),
            mock.patch(
                "apps.core.models.user_acquisition.timezone.now", return_value=timezone.now() + timedelta(seconds=60)
            ),
        ):
            response = self.client.get(self._signed(grant), headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 410)

    def test_a_missing_token_takes_the_full_path(self):
        grant = self._grant()
        self._open(grant)
        cache.delete(f"revocation:{revocation.GRANT}:{grant.pk}")
        with mock.patch.object(views, "_load_user_acquisition", wraps=views._load_user_acquisition) as loaded:
            response = self.client.get(self._signed(grant), headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 206)
        loaded.assert_called()
        # The full path minted a new token and re-pinned: the next range is fast again.
        with self.assertNumQueries(0):
            self.assertEqual(self.client.get(self._signed(grant), headers={"Range": "bytes=0-9"}).status_code, 206)

    def test_a_pin_older_than_its_maximum_age_takes_the_full_path(self):
        grant = self._grant()
        self._open(grant)
        with mock.patch.object(personalised.time, "time", return_value=time.time() + personalised.PIN_MAX_SECONDS + 1):
            self.assertIsNone(personalised.pinned(grant.pk, ""))

    def test_facts_that_moved_while_pinning_are_not_pinned(self):
        grant = UserAcquisition.objects.select_related("acquisition__entry", "user").get(pk=self._grant().pk)
        UserAcquisition.objects.filter(pk=grant.pk).update(expire_at=timezone.now() + timedelta(days=1))
        self.assertIsNone(views.UserAcquisitionDownload._access_facts(grant))

    def test_a_cache_outage_never_breaks_a_save(self):
        # Revocation runs on commit; a failing cache is logged, the save and other on-commit hooks go on.
        with mock.patch.object(revocation.cache, "delete", side_effect=ConnectionError("redis down")):
            with self.captureOnCommitCallbacks(execute=True):
                self.reader.name = "Renamed"
                self.reader.save()
        self.reader.refresh_from_db()
        self.assertEqual(self.reader.name, "Renamed")

    def test_the_admin_deactivation_action_revokes(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        grant = self._grant()
        self._open(grant)
        user_admin = site._registry[User]
        request = RequestFactory().post("/")
        request.user = self.manager
        user_admin.message_user = mock.Mock()
        with self.captureOnCommitCallbacks(execute=True):
            user_admin.deactivate(request, User.objects.filter(pk=self.reader.pk))
        response = self.client.get(self._signed(grant), headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 403)

    # --- DELETE -------------------------------------------------------------------------------------------

    def _delete(self, grant, user):
        return self.client.delete(
            reverse("api:user-acquisition-detail", kwargs={"user_acquisition_id": grant.pk}),
            headers=self._headers(user),
        )

    def test_delete_expires_the_grant_and_keeps_its_annotations(self):
        grant = self._grant()
        annotation = Annotation.objects.create(user_acquisition=grant, title="Kept")
        self.assertEqual(self._delete(grant, self.reader).status_code, 204)
        grant.refresh_from_db()
        self.assertTrue(grant.is_expired)
        self.assertTrue(Annotation.objects.filter(pk=annotation.pk).exists())
        self.assertEqual(self.client.get(self._url(grant), headers=self._headers(self.reader)).status_code, 410)
        # Revoking again changes nothing.
        expired_at = grant.expire_at
        self.assertEqual(self._delete(grant, self.reader).status_code, 204)
        grant.refresh_from_db()
        self.assertEqual(grant.expire_at, expired_at)

    def test_only_the_holder_or_a_catalog_manager_may_delete(self):
        grant = self._grant()
        self.assertEqual(self._delete(grant, self.other).status_code, 403)
        self.assertEqual(self._delete(grant, self.manager).status_code, 204)

    # --- E: pre-render --------------------------------------------------------------------------------------

    def test_a_prerendered_copy_is_not_rendered_again_on_the_first_get(self):
        grant = self._grant()
        self.assertTrue(copies.prerender(grant))
        with mock.patch.object(PDFModifier, "generate", autospec=True, side_effect=PDFModifier.generate) as generate:
            response = self.client.get(self._url(grant), headers=self._headers(self.reader))
        self.assertEqual(response.status_code, 200)
        generate.assert_not_called()

    def test_a_new_personal_grant_queues_its_prerender_after_commit(self):
        mock.patch.stopall()
        mock.patch.object(Acquisition._meta.get_field("content"), "storage", get_storage()).start()
        mock.patch.object(personalised, "queue_base").start()
        with mock.patch("apps.tasks.tasks.prerender_personalised_copy.apply_async") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                created = self._post(self.reader)
            self.assertEqual(created.status_code, 201)
            queued.assert_called_once()
            self.assertEqual(queued.call_args.kwargs["countdown"], copies.PRERENDER_COUNTDOWN)

            with self.captureOnCommitCallbacks(execute=True):
                self._grant(type=UserAcquisition.UserAcquisitionType.SHARED)
            queued.assert_called_once()  # shared grants are not rendered ahead

    @override_settings(EVILFLOWERS_ENFORCE_USER_ACQUISITIONS=True)
    def test_acquisition_download_never_queues_a_prerender(self):
        mock.patch.stopall()
        mock.patch.object(Acquisition._meta.get_field("content"), "storage", get_storage()).start()
        mock.patch.object(personalised, "queue_base").start()
        with mock.patch("apps.tasks.tasks.prerender_personalised_copy.apply_async") as queued:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.get(
                    reverse("files:acquisition-download", kwargs={"acquisition_id": self.acquisition.pk}),
                    headers=self._headers(self.reader),
                )
        self.assertEqual(response.status_code, 303)
        queued.assert_not_called()
