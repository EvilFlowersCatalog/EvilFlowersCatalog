"""HTTP Range on stored files (#70)."""

import shutil
import tempfile
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.http import FileResponse, HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.api.middleware.gzip import GZipMiddleware
from apps.core.models import AuthSource, Catalog, Entry, User
from apps.files.ranges import Unsatisfiable, is_continuation, parse_range, ranged_file_response

BODY = bytes(range(256)) * 8  # 2048 bytes


def _body(response) -> bytes:
    return b"".join(response.streaming_content)


class ParseRangeTests(SimpleTestCase):
    def test_forms(self):
        self.assertEqual(parse_range("bytes=0-1023", 2048), (0, 1023))
        self.assertEqual(parse_range("bytes=1000-", 2048), (1000, 2047))
        self.assertEqual(parse_range("bytes=-100", 2048), (1948, 2047))
        self.assertEqual(parse_range("bytes=2000-9999", 2048), (2000, 2047))
        self.assertEqual(parse_range("bytes=-9999", 2048), (0, 2047))

    def test_ignored(self):
        for header in (None, "", "bytes=0-1,5-6", "items=0-5", "bytes=10-5", "bytes=-", "nonsense"):
            with self.subTest(header=header):
                self.assertIsNone(parse_range(header, 2048))

    def test_unsatisfiable(self):
        for header in ("bytes=2048-", "bytes=5000-6000", "bytes=-0"):
            with self.subTest(header=header):
                with self.assertRaises(Unsatisfiable):
                    parse_range(header, 2048)
        with self.assertRaises(Unsatisfiable):
            parse_range("bytes=-10", 0)

    def test_continuation(self):
        factory = RequestFactory()
        self.assertFalse(is_continuation(factory.get("/")))
        self.assertFalse(is_continuation(factory.get("/", HTTP_RANGE="bytes=0-65535")))
        self.assertFalse(is_continuation(factory.get("/", HTTP_RANGE="bytes=0-1,5-6")))
        self.assertTrue(is_continuation(factory.get("/", HTTP_RANGE="bytes=65536-131071")))
        self.assertTrue(is_continuation(factory.get("/", HTTP_RANGE="bytes=-1024")))


class RangedFileResponseTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.storage = FileSystemStorage(location=self.root)
        self.name = self.storage.save("catalogs/x/book.pdf", ContentFile(BODY))
        self.factory = RequestFactory()

    def serve(self, **headers):
        return ranged_file_response(
            self.factory.get("/", **headers),
            self.storage,
            self.name,
            content_type="application/pdf",
            filename="book.pdf",
            as_attachment=True,
        )

    def test_whole_file_advertises_ranges(self):
        response = self.serve()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Length"], "2048")
        self.assertEqual(_body(response), BODY)

    def test_first_kilobyte(self):
        response = self.serve(HTTP_RANGE="bytes=0-1023")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 0-1023/2048")
        self.assertEqual(response["Content-Length"], "1024")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('attachment; filename="book.pdf"', response["Content-Disposition"])
        self.assertEqual(_body(response), BODY[:1024])

    def test_middle_and_suffix(self):
        self.assertEqual(_body(self.serve(HTTP_RANGE="bytes=100-199")), BODY[100:200])
        response = self.serve(HTTP_RANGE="bytes=-48")
        self.assertEqual(response["Content-Range"], "bytes 2000-2047/2048")
        self.assertEqual(_body(response), BODY[-48:])

    def test_unsatisfiable(self):
        response = self.serve(HTTP_RANGE="bytes=4096-")
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */2048")

    def test_if_range_serves_whole_file(self):
        response = self.serve(HTTP_RANGE="bytes=0-9", HTTP_IF_RANGE='"abc"')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_body(response), BODY)

    def test_open_range_backend(self):
        """S3: the backend's `open_range` is asked for exactly the bytes, nothing seeks."""
        stream = mock.Mock()
        stream.read.side_effect = [BODY[10:20], b""]
        self.storage.open_range = mock.Mock(return_value=stream)

        response = self.serve(HTTP_RANGE="bytes=10-19")

        self.assertEqual(_body(response), BODY[10:20])
        self.storage.open_range.assert_called_once_with(self.name, 10, 10)
        stream.seek.assert_not_called()
        stream.close.assert_called_once()


class CoverEndpointTests(TestCase):
    """Through the whole middleware stack: gzip must not swallow Content-Length or the 206."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(
            EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root, EVILFLOWERS_EVENT_BROKER_EXECUTOR=None
        )
        override.enable()
        self.addCleanup(override.disable)

        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        user = User(username="ranger", name="Ra", surname="Nger", auth_source=auth_source)
        user.set_unusable_password()
        user.save()
        catalog = Catalog.objects.create(creator=user, title="Ranged", url_name="ranged")
        self.entry = Entry.objects.create(creator=user, catalog=catalog, title="Ranged", image_mime="image/jpeg")
        self.entry.image.save("cover.jpg", ContentFile(BODY), save=True)

    def test_range_and_whole(self):
        url = reverse("files:cover-download", kwargs={"entry_id": self.entry.pk})

        whole = self.client.get(url, HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(whole["Accept-Ranges"], "bytes")
        self.assertEqual(whole["Content-Length"], "2048")
        self.assertFalse(whole.has_header("Content-Encoding"))

        part = self.client.get(url, HTTP_RANGE="bytes=0-1023", HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part["Content-Range"], "bytes 0-1023/2048")
        self.assertEqual(b"".join(part.streaming_content), BODY[:1024])


class GZipSkipsFilesTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/", HTTP_ACCEPT_ENCODING="gzip")
        self.middleware = GZipMiddleware(lambda request: None)

    def test_file_response_keeps_content_length(self):
        response = FileResponse(ContentFile(BODY, name="book.pdf"))
        response["Content-Length"] = "2048"
        response = self.middleware.process_response(self.request, response)
        self.assertFalse(response.has_header("Content-Encoding"))
        self.assertEqual(response["Content-Length"], "2048")

    def test_other_responses_still_compressed(self):
        response = self.middleware.process_response(self.request, HttpResponse(b"a" * 1000))
        self.assertEqual(response["Content-Encoding"], "gzip")
