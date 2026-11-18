"""Acquisition uploads and downloads that cannot be a publication (#112), and Range end to end (#70)."""

import shutil
import tempfile
import uuid
from io import BytesIO
from unittest import mock

import fitz
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.api.services.entry import InvalidAcquisitionFile, check_acquisition_file
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User, UserAcquisition
from apps.core.modifiers.pdf import PDFModifier
from apps.files import personalised, signing
from apps.files.services import AcquisitionStorageError, AcquisitionStorageService
from apps.files.storage import get_storage

EPUB = b"PK\x03\x04" + bytes(range(256)) * 8


class CheckAcquisitionFileTests(SimpleTestCase):
    def test_empty_file_is_a_bad_request(self):
        with self.assertRaises(InvalidAcquisitionFile) as raised:
            check_acquisition_file(SimpleUploadedFile("a.pdf", b"", "application/pdf"), "application/pdf")
        self.assertEqual(raised.exception.status, 400)

    def test_wrong_magic_is_unsupported_media_type(self):
        with self.assertRaises(InvalidAcquisitionFile) as raised:
            check_acquisition_file(SimpleUploadedFile("a.pdf", b"<html>", "application/pdf"), "application/pdf")
        self.assertEqual(raised.exception.status, 415)
        with self.assertRaises(InvalidAcquisitionFile):
            check_acquisition_file(SimpleUploadedFile("a.epub", b"%PDF-1.7", "x"), "application/epub+zip")

    def test_good_files_pass_and_are_rewound(self):
        upload = SimpleUploadedFile("a.pdf", b"%PDF-1.7\n...", "application/pdf")
        check_acquisition_file(upload, "application/pdf")
        self.assertEqual(upload.read(5), b"%PDF-")
        # A type without a known signature is only checked for being non-empty.
        check_acquisition_file(BytesIO(b"anything"), "application/x-mobipocket-ebook")


@override_settings(
    EVILFLOWERS_EVENT_BROKER_EXECUTOR=None,
    EVILFLOWERS_MODIFIERS={"application/pdf": "apps.core.modifiers.pdf.PDFModifier"},
)
class AcquisitionDownloadTests(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        # No broker in tests: the shared base is built only where a test runs the worker's job itself.
        self.queue_base_patcher = mock.patch.object(personalised, "queue_base")
        self.queue_base = self.queue_base_patcher.start()
        self.queue_base.stop = self.queue_base_patcher.stop
        self.addCleanup(mock.patch.stopall)

        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        # Superuser: catalog permissions are not what these tests are about.
        self.user = User(username="reader", name="Re", surname="Ader", auth_source=source, is_superuser=True)
        self.user.set_unusable_password()
        self.user.save()
        catalog = Catalog.objects.create(creator=self.user, title="Open", url_name="open", is_public=True)
        self.entry = Entry.objects.create(creator=self.user, catalog=catalog, title="Ranged Book")

    def _acquisition(self, content: bytes, mime=Acquisition.AcquisitionMIME.MOBI) -> Acquisition:
        # MOBI is served as stored; PDF and EPUB are redirected to a personalised copy.
        acquisition = Acquisition.objects.create(entry=self.entry, mime=mime)
        acquisition.content.save("book.bin", ContentFile(content), save=True)
        return acquisition

    def test_an_empty_stored_file_is_a_storage_error_not_a_crash(self):
        acquisition = self._acquisition(b"")
        with self.assertRaises(AcquisitionStorageError):
            AcquisitionStorageService.download_response(acquisition, RequestFactory().get("/"))

    def test_a_file_missing_from_storage_is_a_storage_error(self):
        acquisition = self._acquisition(EPUB)
        acquisition.content.storage.delete(acquisition.content.name)
        with self.assertRaises(AcquisitionStorageError):
            AcquisitionStorageService.download_response(acquisition, RequestFactory().get("/"))

    def _headers(self) -> dict:
        key = ApiKey.objects.create(user=self.user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def test_range_through_the_acquisition_endpoint(self):
        acquisition = self._acquisition(EPUB)
        url = reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})

        # A signed-in reader is redirected to their own copy (EVILFLOWERS_ENFORCE_USER_ACQUISITIONS); follow it.
        whole = self.client.get(url, headers=self._headers(), follow=True)
        self.assertEqual(whole.status_code, 200, whole.redirect_chain)
        self.assertEqual(whole["Accept-Ranges"], "bytes")
        self.assertEqual(whole["Content-Length"], str(len(EPUB)))

        part = self.client.get(url, headers={**self._headers(), "Range": "bytes=0-1023"}, follow=True)
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part["Content-Range"], f"bytes 0-1023/{len(EPUB)}")
        self.assertEqual(b"".join(part.streaming_content), EPUB[:1024])

    def _pdf(self, pages=3) -> bytes:
        document = fitz.open()
        for number in range(pages):
            document.new_page().insert_text((72, 72), f"Page {number + 1}")
        return document.tobytes()

    def _get(self, url, range_=None):
        headers = self._headers()
        if range_:
            headers["Range"] = range_
        return self.client.get(url, headers=headers, follow=True)

    def _body(self, response) -> bytes:
        return b"".join(response.streaming_content)

    def test_personalised_pdf_ranges_come_from_one_stored_render(self):
        acquisition = self._acquisition(self._pdf(), mime=Acquisition.AcquisitionMIME.PDF)
        url = reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})

        with mock.patch.object(PDFModifier, "generate", autospec=True, side_effect=PDFModifier.generate) as generate:
            whole = self._get(url)
            self.assertEqual(whole.status_code, 200, whole.redirect_chain)
            self.assertEqual(whole["Accept-Ranges"], "bytes")
            body = self._body(whole)
            self.assertEqual(whole["Content-Length"], str(len(body)))
            self.assertTrue(body.startswith(b"%PDF-"))

            head = self._get(url, "bytes=0-99")
            tail = self._get(url, "bytes=100-")
        self.assertEqual(generate.call_count, 1)

        self.assertEqual(head.status_code, 206)
        self.assertEqual(head["Content-Range"], f"bytes 0-99/{len(body)}")
        self.assertEqual(tail.status_code, 206)
        self.assertEqual(self._body(head) + self._body(tail), body)

        self.entry.refresh_from_db()
        # #214: only the GET without Range opens the book; the range from byte 0 is part of that open.
        self.assertEqual(self.entry.popularity, 1)

    # --- #214: streaming contract ---------------------------------------------------------------------------

    def _user_acquisition(self, content: bytes, mime=Acquisition.AcquisitionMIME.PDF) -> UserAcquisition:
        acquisition = self._acquisition(content, mime=mime)
        return UserAcquisition.objects.create(
            acquisition=acquisition, user=self.user, type=UserAcquisition.UserAcquisitionType.PERSONAL
        )

    def _ua_url(self, user_acquisition, query="") -> str:
        url = reverse("files:user-acquisition-download", kwargs={"user_acquisition_id": user_acquisition.pk})
        return f"{url}?{query}" if query else url

    def test_one_open_counts_once_and_head_never(self):
        user_acquisition = self._user_acquisition(self._pdf())
        url = self._ua_url(user_acquisition)
        headers = self._headers()

        self.assertEqual(self.client.head(url, headers=headers).status_code, 200)
        opened = self.client.get(url, headers=headers)
        first = self.client.get(url, headers={**headers, "Range": "bytes=0-65535"})
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(first.status_code, 206)

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.popularity, 1)

    def test_the_copy_carries_a_strong_etag_and_honours_if_range(self):
        user_acquisition = self._user_acquisition(self._pdf())
        url = self._ua_url(user_acquisition)
        headers = self._headers()

        whole = self.client.get(url, headers=headers)
        etag = whole["ETag"]
        body = self._body(whole)
        self.assertRegex(etag, r'^"[0-9a-f]{24}-\d+"$')
        self.assertTrue(etag.endswith(f'-{len(body)}"'))
        self.assertIn("Last-Modified", whole.headers)

        part = self.client.get(url, headers={**headers, "Range": "bytes=0-9", "If-Range": etag})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part["ETag"], etag)
        stale = self.client.get(url, headers={**headers, "Range": "bytes=0-9", "If-Range": '"other-1"'})
        self.assertEqual(stale.status_code, 200)

        head = self.client.head(url, headers=headers)
        self.assertEqual(head["ETag"], etag)
        self.assertEqual(head["Content-Length"], str(len(body)))

    def test_ranges_read_the_pin_not_the_copy_name(self):
        user_acquisition = self._user_acquisition(self._pdf())
        url = self._ua_url(user_acquisition)
        headers = self._headers()
        body = self._body(self.client.get(url, headers=headers))

        with mock.patch.object(personalised, "copy_name", wraps=personalised.copy_name) as copy_name:
            parts = [
                self._body(self.client.get(url, headers={**headers, "Range": f"bytes={start}-{start + 99}"}))
                for start in range(0, 300, 100)
            ]
        copy_name.assert_not_called()
        self.assertEqual(b"".join(parts), body[:300])

        # A new open (GET without Range) resolves the copy again: an edited annotation shows.
        with mock.patch.object(personalised, "copy_name", wraps=personalised.copy_name) as copy_name:
            self.client.get(url, headers=headers)
        copy_name.assert_called_once()

    def test_an_evicted_copy_is_resolved_again(self):
        user_acquisition = self._user_acquisition(self._pdf())
        url = self._ua_url(user_acquisition)
        headers = self._headers()
        first = self.client.get(url, headers=headers)
        storage = user_acquisition.acquisition.content.storage
        _folders, files = storage.listdir(f"personalised/{user_acquisition.pk}")
        self.assertEqual(len(files), 1)
        cache.delete(personalised._ready_key(f"personalised/{user_acquisition.pk}/{files[0]}"))

        with mock.patch.object(personalised, "copy_name", wraps=personalised.copy_name) as copy_name:
            part = self.client.get(url, headers={**headers, "Range": "bytes=0-9"})
        self.assertEqual(part.status_code, 206)
        copy_name.assert_called_once()  # the pin was dropped with the copy's marker
        # Rendered again under the same name, with other bytes (`generated_at`): the ETag must differ,
        # or an If-Range would approve ranges of two renders.
        self.assertNotEqual(part["ETag"], first["ETag"])
        stale = self.client.get(url, headers={**headers, "Range": "bytes=0-9", "If-Range": first["ETag"]})
        self.assertEqual(stale.status_code, 200)

    def test_signed_url_reads_without_a_token(self):
        user_acquisition = self._user_acquisition(self._pdf())
        detail = self.client.get(
            reverse("api:user-acquisition-detail", kwargs={"user_acquisition_id": user_acquisition.pk}),
            headers=self._headers(),
        )
        self.assertEqual(detail.status_code, 200, detail.content[:300])
        response = detail.json()["response"]
        stream_url = response["stream_url"]
        self.assertIn("sig=", stream_url)
        self.assertIsNotNone(response["stream_url_expires_at"])

        whole = self.client.get(stream_url)
        self.assertEqual(whole.status_code, 200, whole.content[:300] if not whole.streaming else "")
        body = self._body(whole)
        self.assertTrue(body.startswith(b"%PDF-"))
        part = self.client.get(stream_url, headers={"Range": "bytes=0-99"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(self._body(part), body[:100])
        # The same personalised copy as the token-authenticated reader's: same ETag.
        self.assertEqual(
            self.client.get(self._ua_url(user_acquisition), headers=self._headers())["ETag"], whole["ETag"]
        )

    def test_a_bad_or_expired_signature_is_refused(self):
        user_acquisition = self._user_acquisition(self._pdf())
        query, _expires = signing.signed_query(user_acquisition)
        other = self._user_acquisition(self._pdf())
        # A signature names one user acquisition only.
        self.assertEqual(self.client.get(self._ua_url(other, query)).status_code, 403)
        self.assertEqual(self.client.get(self._ua_url(user_acquisition, query + "0")).status_code, 403)
        expired, _ = signing.signed_query(user_acquisition, now=0)
        self.assertEqual(self.client.get(self._ua_url(user_acquisition, expired)).status_code, 403)
        self.assertEqual(self.client.get(self._ua_url(user_acquisition, query)).status_code, 200)

    def test_signed_url_is_for_the_owner_only(self):
        user_acquisition = self._user_acquisition(self._pdf())
        stranger = User(username="stranger", name="S", surname="T", auth_source=self.user.auth_source)
        stranger.set_unusable_password()
        stranger.save()
        UserAcquisition.objects.filter(pk=user_acquisition.pk).update(type=UserAcquisition.UserAcquisitionType.SHARED)
        key = ApiKey.objects.create(user=stranger, name="test")
        response = self.client.get(
            reverse("api:user-acquisition-detail", kwargs={"user_acquisition_id": user_acquisition.pk}),
            headers={"Authorization": f"Bearer {JWTFactory(stranger.pk).api_key(str(key.pk))}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["response"]["stream_url"])

    @override_settings(CORS_ALLOWED_ORIGINS=["https://portal.example"])
    def test_range_headers_are_exposed_to_the_portal(self):
        user_acquisition = self._user_acquisition(EPUB, mime=Acquisition.AcquisitionMIME.MOBI)
        response = self.client.get(
            self._ua_url(user_acquisition),
            headers={**self._headers(), "Range": "bytes=0-9", "Origin": "https://portal.example"},
        )
        self.assertEqual(response.status_code, 206)
        exposed = {name.strip().lower() for name in response["Access-Control-Expose-Headers"].split(",")}
        self.assertTrue({"accept-ranges", "content-range", "etag", "content-disposition"} <= exposed)

    def test_pdf_is_never_compressed(self):
        user_acquisition = self._user_acquisition(self._pdf())
        headers = {**self._headers(), "Accept-Encoding": "gzip"}
        for extra in ({}, {"Range": "bytes=0-9"}):
            response = self.client.get(self._ua_url(user_acquisition), headers={**headers, **extra})
            self.assertNotIn("Content-Encoding", response.headers)

    def test_copies_share_one_normalised_base(self):
        first = self._user_acquisition(self._pdf())
        readers = [first] + [
            UserAcquisition.objects.create(
                acquisition=first.acquisition, user=self.user, type=UserAcquisition.UserAcquisitionType.PERSONAL
            )
            for _ in range(2)
        ]
        # The worker's job, run inline: `queue_base` hands the build to Celery in production.
        self.queue_base.side_effect = personalised.build_base
        with (
            mock.patch.object(PDFModifier, "normalise", autospec=True, side_effect=PDFModifier.normalise) as base,
            mock.patch.object(PDFModifier, "generate", autospec=True, side_effect=PDFModifier.generate) as generate,
        ):
            bodies = [self._body(self.client.get(self._ua_url(ua), headers=self._headers())) for ua in readers]
        self.assertEqual(base.call_count, 1)
        # The first reader never waits for the base: rendered in full from the source, as before.
        self.assertEqual([call.kwargs["normalised"] for call in generate.call_args_list], [False, True, True])
        self.assertEqual(self.queue_base.call_count, 1)
        self.assertEqual(len(set(bodies)), 3)  # each copy is personal
        for body in bodies:
            self.assertEqual(len(fitz.open(stream=body)), 4)  # licence page inserted after the first page

    def test_a_queue_failure_never_fails_the_read(self):
        self.queue_base.stop()
        user_acquisition = self._user_acquisition(self._pdf())
        with mock.patch(
            "apps.tasks.tasks.build_personalised_base.apply_async", side_effect=ConnectionError("broker down")
        ) as queued:
            response = self.client.get(self._ua_url(user_acquisition), headers=self._headers())
            again = self.client.get(self._ua_url(user_acquisition), headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(again.status_code, 200)
        queued.assert_called_once()  # queued once per file however many readers ask meanwhile

    @override_settings(EVILFLOWERS_PERSONALISED_SHARED_BASE=False)
    def test_without_the_shared_base_a_copy_is_rendered_in_full(self):
        user_acquisition = self._user_acquisition(self._pdf())
        with mock.patch.object(PDFModifier, "normalise", autospec=True) as base:
            response = self.client.get(self._ua_url(user_acquisition), headers=self._headers())
        base.assert_not_called()
        self.assertTrue(self._body(response).startswith(b"%PDF-"))

    def test_a_page_range_leaves_no_trace_of_other_pages(self):
        document = fitz.open()
        for number in range(3):
            page = document.new_page()
            page.insert_text((72, 72), f"SECRET-PAGE-{number + 1}")
        user_acquisition = self._user_acquisition(document.tobytes())
        UserAcquisition.objects.filter(pk=user_acquisition.pk).update(range="1-1")
        response = self.client.get(self._ua_url(user_acquisition), headers=self._headers())
        self.assertEqual(response.status_code, 200)

        copy = fitz.open(stream=self._body(response))
        text = "".join(page.get_text() for page in copy)
        self.assertIn("SECRET-PAGE-1", text)
        self.assertNotIn("SECRET-PAGE-2", text)
        # No unreferenced leftover of the pages the range excluded, in either string form of a content stream.
        streams = b"".join(copy.xref_stream(xref) or b"" for xref in range(1, copy.xref_length()))

        def found(text: str) -> bool:
            return text.encode() in streams or text.encode().hex().encode() in streams

        self.assertTrue(found("SECRET-PAGE-1"))
        self.assertFalse(found("SECRET-PAGE-2"))
        self.assertFalse(found("SECRET-PAGE-3"))

    def test_annotations_name_a_new_copy(self):
        acquisition = self._acquisition(self._pdf(), mime=Acquisition.AcquisitionMIME.PDF)
        user_acquisition = UserAcquisition.objects.create(
            acquisition=acquisition, user=self.user, type=UserAcquisition.UserAcquisitionType.PERSONAL
        )
        plain = personalised.copy_name(user_acquisition, {"id": "x"}, None, {})
        self.assertEqual(plain, personalised.copy_name(user_acquisition, {"id": "x"}, None, {}))
        self.assertNotEqual(plain, personalised.copy_name(user_acquisition, {"id": "x"}, None, {1: ["<svg/>"]}))
        self.assertNotEqual(plain, personalised.copy_name(user_acquisition, {"id": "x"}, "2", {}))
        self.assertTrue(plain.startswith(f"personalised/{user_acquisition.pk}/"))

    def test_an_unmarked_copy_is_rendered_again_and_idle_copies_are_deleted(self):
        storage = get_storage()
        name = f"personalised/{uuid.uuid4()}/copy.pdf"
        storage.save(name, ContentFile(b"half-written"))

        self.assertTrue(personalised.ensure_copy(storage, name, lambda: ContentFile(b"%PDF-complete")))
        with storage.open(name) as stored:
            self.assertEqual(stored.read(), b"%PDF-complete")

        with mock.patch.object(personalised, "LOCK_SECONDS", -60):
            self.assertEqual(personalised.cleanup_idle(storage), 0)  # still marked as used
            cache.delete(personalised._ready_key(name))
            self.assertEqual(personalised.cleanup_idle(storage), 1)
        self.assertFalse(storage.exists(name))

    def test_an_empty_file_downloads_as_not_found(self):
        acquisition = self._acquisition(b"")
        response = self.client.get(
            reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk}),
            headers=self._headers(),
            follow=True,
        )
        self.assertEqual(response.status_code, 404)

    def test_an_empty_pdf_is_gone_not_a_server_error(self):
        # The reported case: GET /data/v1/acquisitions/{id} of a 0-byte PDF answered 500 (#112).
        acquisition = self._acquisition(b"", mime=Acquisition.AcquisitionMIME.PDF)
        url = reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})
        response = self.client.get(url, headers=self._headers(), follow=True)
        self.assertEqual(response.status_code, 410, response.content[:300])
