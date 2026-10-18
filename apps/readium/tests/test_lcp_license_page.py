"""
Regression test: LCP-protected PDFs must carry the license page.

Personal downloads get the license page from `PDFModifier.generate` at download
time, but the LCP worker used to encrypt the raw acquisition file, so borrowed
copyrighted books shipped without any license text. The encryption source is
now a copy with the license page inserted (and no per-user QR watermark — the
package is shared by every borrower).
"""

import io
from unittest.mock import MagicMock, patch

import fitz
from django.core.files import File
from django.test import SimpleTestCase, override_settings

from apps.core.modifiers.pdf import PDFModifier
from apps.readium.models import EncryptedContent
from apps.readium.services.content_encryption_service import ContentEncryptionService


def _pdf_bytes(pages: int = 2) -> bytes:
    document = fitz.open()
    for index in range(pages):
        document.new_page().insert_text((72, 72), f"Body page {index + 1}")
    return document.tobytes()


class LicenseCopyTests(SimpleTestCase):
    def test_inserts_license_page_without_watermark(self):
        modifier = PDFModifier(context={"title": "Kniha", "authors": "Autor", "language": None}, pages=None)

        result = fitz.open(stream=modifier.license_copy(File(io.BytesIO(_pdf_bytes(2)))).read())

        self.assertEqual(len(result), 3)
        self.assertIn("Body page 1", result[0].get_text())
        license_text = result[1].get_text()
        self.assertIn("Copyright Notice", license_text)
        self.assertIn("Upozornenie o autorských právach", license_text)
        self.assertIn("Kniha", license_text)
        # No borrower is known at encryption time — no empty user fields.
        self.assertNotIn("User / Používateľ", license_text)
        self.assertNotIn("Document ID", license_text)
        self.assertNotIn("Generated at", license_text)
        self.assertNotIn("None", license_text)
        self.assertEqual(result[2].get_images(), [])

    def test_personal_download_keeps_user_fields_and_watermark(self):
        modifier = PDFModifier(
            context={"id": "doc-1", "user_id": "u-1", "username": "reader", "title": "Kniha", "language": None},
            pages=None,
        )

        result = fitz.open(stream=modifier.generate(File(io.BytesIO(_pdf_bytes(2)))).read())

        self.assertEqual(len(result), 3)
        license_text = result[1].get_text()
        self.assertIn("User / Používateľ: reader (u-1)", license_text)
        self.assertIn("Document ID / Identifikátor dokumentu: doc-1", license_text)
        self.assertIn("Generated at", license_text)
        self.assertNotEqual(result[2].get_images(), [])


@override_settings(
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_MODIFIERS={"application/pdf": "apps.core.modifiers.pdf.PDFModifier"},
)
class EncryptionSourceTests(SimpleTestCase):
    def _make_encrypted_content(self, mime: str):
        entry = MagicMock()
        entry.title = "Kniha"
        entry.first_author_name = "Autor"
        entry.language = None
        entry.authors.all.return_value = []

        acquisition = MagicMock()
        acquisition.entry = entry
        acquisition.mime = mime
        acquisition.content.name = "catalogs/stu/entry/book.pdf"
        acquisition.content.read.return_value = _pdf_bytes(1)
        acquisition.upload_base_path.return_value = "catalogs/stu/entry"

        encrypted_content = MagicMock()
        encrypted_content.acquisition = acquisition
        encrypted_content.lcp_content_id = "content-123"
        return encrypted_content

    def _queue(self, encrypted_content, storage):
        with (
            patch("apps.readium.services.content_encryption_service.get_event_broker") as mock_get_broker,
            patch("apps.readium.services.content_encryption_service.get_storage", return_value=storage),
        ):
            broker = MagicMock()
            mock_get_broker.return_value = broker
            ContentEncryptionService._queue_encryption_task(encrypted_content)
            return broker

    def test_pdf_is_encrypted_from_licensed_copy(self):
        storage = MagicMock()
        storage.exists.return_value = False
        storage.save.side_effect = lambda name, content: name

        broker = self._queue(self._make_encrypted_content("application/pdf"), storage)

        source_name, source_file = storage.save.call_args.args
        self.assertEqual(source_name, "catalogs/stu/entry/encrypted/source/content-123/book.pdf")
        self.assertEqual(len(fitz.open(stream=source_file.read())), 2)

        _event, payload = broker.execute.call_args.args
        self.assertEqual(payload["kwargs"]["input_file"], source_name)

    def test_epub_is_encrypted_as_is(self):
        storage = MagicMock()
        encrypted_content = self._make_encrypted_content("application/epub+zip")

        broker = self._queue(encrypted_content, storage)

        storage.save.assert_not_called()
        _event, payload = broker.execute.call_args.args
        self.assertEqual(payload["kwargs"]["input_file"], "catalogs/stu/entry/book.pdf")

    def test_failed_stamping_does_not_queue_raw_file(self):
        storage = MagicMock()
        encrypted_content = self._make_encrypted_content("application/pdf")
        encrypted_content.acquisition.content.read.return_value = b"not a pdf"

        broker = self._queue(encrypted_content, storage)

        broker.execute.assert_not_called()
        self.assertEqual(encrypted_content.status, EncryptedContent.EncryptionStatus.FAILED)
        self.assertIn("license page", encrypted_content.error_message)


class ReencryptGuardTests(SimpleTestCase):
    def test_refuses_with_active_loans(self):
        encrypted_content = MagicMock()
        with (
            patch.object(ContentEncryptionService, "active_licenses") as active,
            patch.object(ContentEncryptionService, "_queue_encryption_task") as queue,
        ):
            active.return_value.exists.return_value = True
            with self.assertRaises(ValueError):
                ContentEncryptionService.reencrypt(encrypted_content)
            queue.assert_not_called()

    def test_force_resets_row_and_queues(self):
        encrypted_content = MagicMock()
        with (
            patch.object(ContentEncryptionService, "active_licenses") as active,
            patch.object(ContentEncryptionService, "_queue_encryption_task") as queue,
        ):
            active.return_value.exists.return_value = True
            ContentEncryptionService.reencrypt(encrypted_content, force=True)

        self.assertEqual(encrypted_content.status, EncryptedContent.EncryptionStatus.PENDING)
        self.assertIsNone(encrypted_content.encrypted_at)
        queue.assert_called_once_with(encrypted_content)
