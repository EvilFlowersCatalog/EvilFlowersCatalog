"""The Readium encryption operations tools, and who may use them.

Both tools are administrator-only. The point worth pinning down is that a
catalog manager — who may curate everything in the catalog — is still refused,
and that a refusal names no job.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.core.models import Acquisition, Entry
from apps.mcp.tests.test_management import ManagementTestCase
from apps.readium.models import EncryptedContent, License

BROKER = "apps.readium.services.content_encryption_service.get_event_broker"


@override_settings(
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_READIUM_LCPSV_URL="http://lcp.example.org",
)
class EncryptionTestCase(ManagementTestCase):
    def setUp(self):
        super().setUp()
        # The jobs' acquisition files never exist on disk; inserting the
        # license page is covered by apps.readium.tests.test_lcp_license_page.
        patcher = patch(
            "apps.readium.services.content_encryption_service.ContentEncryptionService._prepare_source",
            side_effect=lambda encrypted_content: encrypted_content.acquisition.content.name,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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


class EncryptionToolTests(EncryptionTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pending = cls._job(EncryptedContent.EncryptionStatus.PENDING)
        cls.failed = cls._job(EncryptedContent.EncryptionStatus.FAILED, error_message="lcpencrypt exited 1")
        cls.registered = cls._job(EncryptedContent.EncryptionStatus.REGISTERED)

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

    def test_listing_carries_dispatch_tracking(self):
        with patch(BROKER):
            requeued = self.ok("requeue_encryption", {"content_ids": [self.pending.lcp_content_id]}, self.superuser)

        (item,) = [
            item
            for item in self.ok("list_encryption_jobs", {}, self.superuser)["items"]
            if item["content_id"] == self.pending.lcp_content_id
        ]
        self.assertEqual(item["task_id"], requeued["results"][0]["task_id"])
        self.assertEqual(item["attempts"], 1)
        self.assertIsNotNone(item["last_dispatched_at"])
        self.assertIsNone(item["report"])
        self.assertFalse(item["stale"])

    @override_settings(EVILFLOWERS_READIUM_ENCRYPTION_STALE_MINUTES=15)
    def test_a_pending_job_without_a_report_turns_stale(self):
        long_ago = timezone.now() - timedelta(minutes=16)
        EncryptedContent.objects.filter(pk=self.pending.pk).update(last_dispatched_at=long_ago)
        EncryptedContent.objects.filter(pk=self.failed.pk).update(last_dispatched_at=long_ago)

        items = {item["content_id"]: item for item in self.ok("list_encryption_jobs", {}, self.superuser)["items"]}

        self.assertTrue(items[self.pending.lcp_content_id]["stale"])
        # Only `pending` can be lost; a `failed` job has its answer.
        self.assertFalse(items[self.failed.lcp_content_id]["stale"])

        EncryptedContent.objects.filter(pk=self.pending.pk).update(reported_at=timezone.now())
        items = {item["content_id"]: item for item in self.ok("list_encryption_jobs", {}, self.superuser)["items"]}
        self.assertFalse(items[self.pending.lcp_content_id]["stale"])

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

        # Each result names the Celery task it dispatched.
        sent_task_ids = [call.args[1]["task_id"] for call in get_broker.return_value.execute.call_args_list]
        self.assertEqual([result["task_id"] for result in payload["results"]], sent_task_ids)

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


class ReencryptToolTests(EncryptionTestCase):
    """`reencrypt_publication` (issue #98): re-run a job that already succeeded."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.registered = cls._job(EncryptedContent.EncryptionStatus.REGISTERED)
        cls.lent = cls._job(EncryptedContent.EncryptionStatus.REGISTERED)
        # After the acquisitions (their signals save the entry) and as a
        # queryset update (saving it would fire the LCP toggle signal).
        Entry.objects.filter(pk=cls.entry.pk).update(config={"readium_enabled": True})
        now = timezone.now()
        for user, state in ((cls.reader, License.LicenseState.ACTIVE), (cls.manager, License.LicenseState.READY)):
            License.objects.create(
                entry=cls.entry,
                user=user,
                encrypted_content=cls.lent,
                state=state,
                starts_at=now,
                expires_at=now + timedelta(days=14),
            )
        # A returned loan is not one that breaks.
        License.objects.create(
            entry=cls.entry,
            user=cls.superuser,
            encrypted_content=cls.registered,
            state=License.LicenseState.RETURNED,
            starts_at=now,
            expires_at=now + timedelta(days=14),
        )

    def test_a_registered_job_is_re_encrypted_under_a_new_task(self):
        with patch(BROKER) as get_broker:
            payload = self.ok(
                "reencrypt_publication", {"content_ids": [self.registered.lcp_content_id]}, self.superuser
            )

        self.assertEqual((payload["requeued"], payload["skipped"]), (1, 0))
        (result,) = payload["results"]
        self.assertEqual(result["previous_status"], "registered")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["active_loans"], 0)

        (call,) = get_broker.return_value.execute.call_args_list
        self.assertEqual(call.args[1]["task_id"], result["task_id"])
        # Same content id: the LCP server updates the content in place.
        self.assertEqual(call.args[1]["kwargs"]["contentid"], self.registered.lcp_content_id)

        self.registered.refresh_from_db()
        self.assertEqual(self.registered.status, EncryptedContent.EncryptionStatus.PENDING)
        self.assertIsNone(self.registered.encrypted_at)
        self.assertEqual(self.registered.task_id, result["task_id"])
        self.assertEqual(self.registered.attempts, 1)

    def test_entry_ids_select_every_job_of_the_publication(self):
        with patch(BROKER):
            payload = self.ok(
                "reencrypt_publication", {"entry_ids": [str(self.entry.pk)], "dry_run": True}, self.superuser
            )

        self.assertEqual(
            [result["content_id"] for result in payload["results"]],
            [self.registered.lcp_content_id, self.lent.lcp_content_id],
        )

    def test_active_loans_block_re_encryption_and_are_counted(self):
        with patch(BROKER) as get_broker:
            payload = self.ok("reencrypt_publication", {"content_ids": [self.lent.lcp_content_id]}, self.superuser)

        (result,) = payload["results"]
        self.assertFalse(result["requeued"])
        self.assertEqual(result["active_loans"], 2)
        self.assertIn("2 active loan(s)", result["reason"])
        get_broker.return_value.execute.assert_not_called()

        self.lent.refresh_from_db()
        self.assertEqual(self.lent.status, EncryptedContent.EncryptionStatus.REGISTERED)

    def test_force_re_encrypts_despite_active_loans(self):
        with patch(BROKER) as get_broker:
            payload = self.ok(
                "reencrypt_publication", {"content_ids": [self.lent.lcp_content_id], "force": True}, self.superuser
            )

        self.assertTrue(payload["results"][0]["requeued"])
        self.assertEqual(get_broker.return_value.execute.call_count, 1)

    def test_a_loan_granted_mid_batch_skips_that_job_only(self):
        # The service re-checks loans; one granted after the count is a per-job skip.
        with (
            patch(BROKER) as get_broker,
            patch(
                "apps.mcp.tools.encryption.ContentEncryptionService.reencrypt",
                side_effect=ValueError("Publication has active loans; re-encrypting would break them"),
            ),
        ):
            payload = self.ok(
                "reencrypt_publication", {"content_ids": [self.registered.lcp_content_id]}, self.superuser
            )

        (result,) = payload["results"]
        self.assertFalse(result["requeued"])
        self.assertIn("active loans", result["reason"])
        get_broker.return_value.execute.assert_not_called()

    def test_dry_run_dispatches_nothing(self):
        before = EncryptedContent.objects.get(pk=self.registered.pk)
        with patch(BROKER) as get_broker:
            payload = self.ok(
                "reencrypt_publication",
                {"content_ids": [self.registered.lcp_content_id, self.lent.lcp_content_id], "dry_run": True},
                self.superuser,
            )

        get_broker.return_value.execute.assert_not_called()
        self.assertTrue(payload["dry_run"])
        self.assertEqual((payload["requeued"], payload["skipped"]), (1, 1))
        would, blocked = payload["results"]
        self.assertTrue(would["requeued"])
        self.assertEqual(would["metadata"]["title"], "A Book")
        self.assertIsNone(would.get("task_id"))
        self.assertEqual(blocked["active_loans"], 2)

        after = EncryptedContent.objects.get(pk=self.registered.pk)
        self.assertEqual(
            (after.status, after.task_id, after.attempts, after.updated_at),
            (before.status, before.task_id, before.attempts, before.updated_at),
        )

    def test_a_publication_with_lcp_switched_off_is_skipped(self):
        Entry.objects.filter(pk=self.entry.pk).update(config={"readium_enabled": False})
        with patch(BROKER) as get_broker:
            payload = self.ok(
                "reencrypt_publication", {"content_ids": [self.registered.lcp_content_id]}, self.superuser
            )

        self.assertFalse(payload["results"][0]["requeued"])
        self.assertIn("switched off", payload["results"][0]["reason"])
        get_broker.return_value.execute.assert_not_called()

    def test_content_ids_and_entry_ids_are_exclusive(self):
        for arguments in (
            {},
            {"content_ids": [self.registered.lcp_content_id], "entry_ids": [str(self.entry.pk)]},
        ):
            with self.subTest(arguments=arguments):
                self.assertIn("either", self.refused("reencrypt_publication", arguments, self.superuser))

    def test_an_entry_without_a_job_is_refused_before_anything_is_sent(self):
        with patch(BROKER) as get_broker:
            message = self.refused(
                "reencrypt_publication", {"entry_ids": [str(self.entry.pk), str(uuid.uuid4())]}, self.superuser
            )

        self.assertIn("does not exist", message)
        get_broker.return_value.execute.assert_not_called()

    def test_a_catalog_manager_may_not_re_encrypt(self):
        with patch(BROKER) as get_broker:
            message = self.refused(
                "reencrypt_publication", {"content_ids": [self.registered.lcp_content_id]}, self.manager
            )

        self.assertIn("administrators", message)
        self.assertNotIn(self.registered.lcp_content_id, message)
        get_broker.return_value.execute.assert_not_called()
