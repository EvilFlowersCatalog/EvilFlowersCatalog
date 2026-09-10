"""
Regression test: publication title/author must reach the encryption worker.

When a borrowed LCP-for-PDF publication (`.lcpdf`) is opened in Thorium, the
reader sources its display title/author from the RWPM `manifest.json` that
lcpencrypt generates inside the encrypted package. A raw PDF carries no
embedded metadata, so unless EFC forwards the Entry's title/author to the
encryption worker, the manifest falls back to the filename (title) and empty
authors — Thorium then shows "no title and no authors available".

This asserts that the payload EFC hands to the lcpencrypt worker in
`ContentEncryptionService._queue_encryption_task` carries the Entry's title
and first author name. The upstream HTTP/Celery hop is mocked via the event
broker so no real worker is contacted.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.readium.services.content_encryption_service import ContentEncryptionService


@override_settings(
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_READIUM_LCPSV_URL="http://user:pass@lcp.example.org",
)
class EncryptionPayloadMetadataTests(SimpleTestCase):
    def _make_encrypted_content(self, *, title, author_name):
        entry = MagicMock()
        entry.title = title
        entry.first_author_name = author_name

        acquisition = MagicMock()
        acquisition.entry = entry
        acquisition.content.name = "catalog/source.pdf"
        acquisition.upload_base_path.return_value = "catalog/base"

        encrypted_content = MagicMock()
        encrypted_content.acquisition = acquisition
        encrypted_content.lcp_content_id = "content-123"
        return encrypted_content

    def _captured_kwargs(self, encrypted_content):
        with patch("apps.readium.services.content_encryption_service.get_event_broker") as mock_get_broker:
            broker = MagicMock()
            mock_get_broker.return_value = broker

            ContentEncryptionService._queue_encryption_task(encrypted_content)

            broker.execute.assert_called_once()
            _event, payload = broker.execute.call_args.args
            return payload["kwargs"]

    def test_payload_includes_title_and_author(self):
        ec = self._make_encrypted_content(title="The Great Gatsby", author_name="F. Scott Fitzgerald")

        kwargs = self._captured_kwargs(ec)

        self.assertEqual(
            kwargs.get("title"),
            "The Great Gatsby",
            "Encryption payload must carry the Entry title so lcpencrypt can "
            "populate the package manifest (Thorium reads title from it).",
        )
        self.assertEqual(
            kwargs.get("author"),
            "F. Scott Fitzgerald",
            "Encryption payload must carry the first author name so lcpencrypt " "can populate the package manifest.",
        )

    def test_missing_author_sends_empty_string_not_crash(self):
        # first_author_name returns "" when the entry has no authors; the
        # payload must still be well-formed (empty author, real title).
        ec = self._make_encrypted_content(title="Anonymous Work", author_name="")

        kwargs = self._captured_kwargs(ec)

        self.assertEqual(kwargs.get("title"), "Anonymous Work")
        self.assertEqual(kwargs.get("author"), "")
