"""Acquisition uploads and downloads that cannot be a publication (#112), and Range end to end (#70)."""

import shutil
import tempfile
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.api.services.entry import InvalidAcquisitionFile, check_acquisition_file
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User
from apps.files.services import AcquisitionStorageError, AcquisitionStorageService

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


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class AcquisitionDownloadTests(TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)

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
