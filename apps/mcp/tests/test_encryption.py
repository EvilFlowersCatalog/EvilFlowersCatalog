"""The Readium encryption operations tools, and who may use them.

Both tools are administrator-only. The point worth pinning down is that a
catalog manager — who may curate everything in the catalog — is still refused,
and that a refusal names no job.
"""

import uuid
from unittest.mock import patch

from django.test import override_settings

from apps.core.models import Acquisition
from apps.mcp.tests.test_management import ManagementTestCase
from apps.readium.models import EncryptedContent

BROKER = "apps.readium.services.content_encryption_service.get_event_broker"


@override_settings(
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_READIUM_LCPSV_URL="http://lcp.example.org",
)
class EncryptionToolTests(ManagementTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pending = cls._job(EncryptedContent.EncryptionStatus.PENDING)
        cls.failed = cls._job(EncryptedContent.EncryptionStatus.FAILED, error_message="lcpencrypt exited 1")
        cls.registered = cls._job(EncryptedContent.EncryptionStatus.REGISTERED)

    @classmethod
    def _job(cls, status, **kwargs) -> EncryptedContent:
        acquisition = Acquisition.objects.create(
            entry=cls.entry, mime=Acquisition.AcquisitionMIME.PDF, content=f"catalogs/managed/{uuid.uuid4()}.pdf"
        )
        content_id = str(uuid.uuid4())
        return EncryptedContent.objects.create(
            acquisition=acquisition,
            status=status,
            lcp_content_id=content_id,
            encrypted_path=f"{acquisition.upload_base_path()}/encrypted/{content_id}.lcpdf",
            **kwargs,
        )

    # list_encryption_jobs --------------------------------------------------

    def test_listing_defaults_to_jobs_that_need_attention(self):
        payload = self.ok("list_encryption_jobs", {}, self.superuser)

        self.assertEqual(
            [item["content_id"] for item in payload["items"]],
            [self.pending.lcp_content_id, self.failed.lcp_content_id],
        )
        self.assertEqual(payload["items"][1]["error_message"], "lcpencrypt exited 1")
        self.assertEqual(payload["items"][0]["catalog"], "managed")

    def test_listing_filters_by_status(self):
        payload = self.ok("list_encryption_jobs", {"status": ["registered"]}, self.superuser)
        self.assertEqual([item["content_id"] for item in payload["items"]], [self.registered.lcp_content_id])

    def test_listing_filters_by_catalog(self):
        payload = self.ok("list_encryption_jobs", {"catalog_id": str(self.other_catalog.pk)}, self.superuser)
        self.assertEqual(payload["items"], [])

    def test_a_catalog_manager_may_not_list_jobs(self):
        message = self.refused("list_encryption_jobs", {}, self.manager)
        self.assertIn("administrators", message)

    # requeue_encryption ----------------------------------------------------

    def test_requeue_resends_pending_and_failed_jobs(self):
        with patch(BROKER) as get_broker:
            payload = self.ok(
                "requeue_encryption",
                {"content_ids": [self.pending.lcp_content_id, self.failed.lcp_content_id]},
                self.superuser,
            )

        self.assertEqual(payload["requeued"], 2)
        self.assertEqual(payload["skipped"], 0)
        self.assertEqual(get_broker.return_value.execute.call_count, 2)

        sent = [call.args[1]["kwargs"]["contentid"] for call in get_broker.return_value.execute.call_args_list]
        self.assertEqual(sent, [self.pending.lcp_content_id, self.failed.lcp_content_id])

        self.failed.refresh_from_db()
        self.assertEqual(self.failed.status, EncryptedContent.EncryptionStatus.PENDING)
        self.assertIsNone(self.failed.error_message)

    def test_requeue_keeps_the_content_id_and_output_path(self):
        before = (self.pending.lcp_content_id, self.pending.encrypted_path)
        with patch(BROKER):
            self.ok("requeue_encryption", {"content_ids": [self.pending.lcp_content_id]}, self.superuser)

        self.pending.refresh_from_db()
        self.assertEqual((self.pending.lcp_content_id, self.pending.encrypted_path), before)

    def test_requeue_never_re_encrypts_a_registered_job(self):
        with patch(BROKER) as get_broker:
            payload = self.ok("requeue_encryption", {"content_ids": [self.registered.lcp_content_id]}, self.superuser)

        self.assertEqual(payload["requeued"], 0)
        self.assertEqual(payload["skipped"], 1)
        self.assertFalse(payload["results"][0]["requeued"])
        get_broker.return_value.execute.assert_not_called()

    def test_an_unknown_content_id_is_refused_before_anything_is_sent(self):
        with patch(BROKER) as get_broker:
            message = self.refused(
                "requeue_encryption",
                {"content_ids": [self.pending.lcp_content_id, str(uuid.uuid4())]},
                self.superuser,
            )

        self.assertIn("does not exist", message)
        get_broker.return_value.execute.assert_not_called()

    def test_a_catalog_manager_may_not_requeue(self):
        with patch(BROKER) as get_broker:
            message = self.refused("requeue_encryption", {"content_ids": [self.pending.lcp_content_id]}, self.manager)

        self.assertIn("administrators", message)
        # Same refusal whatever the id: nothing about the job leaks.
        self.assertNotIn(self.pending.lcp_content_id, message)
        get_broker.return_value.execute.assert_not_called()

    @override_settings(EVILFLOWERS_MCP_ALLOW_WRITE=False)
    def test_requeue_is_withdrawn_with_the_write_surface(self):
        response = self.call("requeue_encryption", {"content_ids": [self.pending.lcp_content_id]}, self.superuser)
        self.assertIn("Unknown tool", response["error"]["message"])
