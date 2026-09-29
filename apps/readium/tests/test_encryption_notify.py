"""
lcpencrypt `-notify` callback wiring.

The encryption webhook is the only thing that flips EncryptedContent to
REGISTERED. These tests pin down both halves of the loop:

- the queued worker task carries `notify` from
  `EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL`, and
- the webhook enforces the Basic credentials embedded in that URL.
"""

import base64
import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.readium.services.content_encryption_service import ContentEncryptionService
from apps.readium.views.hooks import EncryptionWebhook

NOTIFY_URL = "http://lcpencrypt:s3cr%40t@172.28.10.1:8000/readium/v1/hooks/encryption"
CONTENT_ID = "acd594d7-2197-49ef-b19a-3bdb94fdfaf2"


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class QueueEncryptionNotifyTests(SimpleTestCase):
    def _queue(self) -> dict:
        encrypted_content = MagicMock(lcp_content_id=CONTENT_ID)
        encrypted_content.acquisition.content.name = "catalogs/stu/entry/file.pdf"
        encrypted_content.acquisition.upload_base_path.return_value = "catalogs/stu/entry"
        encrypted_content.acquisition.entry.title = "Quantum Computing for Everyone"
        encrypted_content.acquisition.entry.first_author_name = "Chris Bernhardt"

        broker = MagicMock()
        with patch(
            "apps.readium.services.content_encryption_service.get_event_broker",
            return_value=broker,
        ):
            ContentEncryptionService._queue_encryption_task(encrypted_content)

        task_name, payload = broker.execute.call_args.args
        self.assertEqual(task_name, "evilflowers_lcpencrypt_worker.lcpencrypt")
        return payload["kwargs"]

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL=NOTIFY_URL)
    def test_task_carries_notify_url(self):
        self.assertEqual(self._queue()["notify"], NOTIFY_URL)

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL=None)
    def test_task_notify_is_none_when_unset(self):
        # The worker only adds `-notify` when the value is truthy.
        self.assertIsNone(self._queue()["notify"])


@patch("apps.readium.views.hooks.ContentEncryptionService")
class EncryptionWebhookAuthTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, **headers):
        request = self.factory.post(
            "/readium/v1/hooks/encryption",
            data=json.dumps({"uuid": CONTENT_ID}),
            content_type="application/json",
            headers=headers,
        )
        return EncryptionWebhook.as_view()(request)

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL=NOTIFY_URL)
    def test_rejects_missing_credentials(self, service):
        response = self._post()
        self.assertEqual(response.status_code, 401)
        service.mark_encryption_completed.assert_not_called()

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL=NOTIFY_URL)
    def test_rejects_wrong_credentials(self, service):
        response = self._post(authorization=_basic("lcpencrypt", "wrong"))
        self.assertEqual(response.status_code, 401)
        service.mark_encryption_completed.assert_not_called()

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL=NOTIFY_URL)
    def test_accepts_matching_credentials_and_registers(self, service):
        # Password in the URL is percent-encoded (`s3cr%40t`); lcpencrypt sends it decoded.
        response = self._post(authorization=_basic("lcpencrypt", "s3cr@t"))
        self.assertEqual(response.status_code, 200)
        service.mark_encryption_completed.assert_called_once_with(CONTENT_ID)
        service.mark_registered_with_lcp_server.assert_called_once_with(service.mark_encryption_completed.return_value)

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL="http://172.28.10.1:8000/readium/v1/hooks/encryption")
    def test_open_when_notify_url_has_no_credentials(self, service):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        service.mark_registered_with_lcp_server.assert_called_once()

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL="http://lcpencrypt@172.28.10.1:8000/x")
    def test_username_without_password_is_ignored_like_lcpencrypt(self, service):
        # lcpencrypt only extracts credentials when both parts are present, and
        # then sends an empty Basic header — the webhook must not demand one.
        response = self._post(authorization=_basic("", ""))
        self.assertEqual(response.status_code, 200)
