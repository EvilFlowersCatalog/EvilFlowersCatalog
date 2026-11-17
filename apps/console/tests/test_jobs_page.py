"""IP-035: the console's jobs dashboard (`console/jobs/`) and its JSON endpoints."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuthSource, Catalog, Entry, Language, User
from apps.tasks.application.facade import facade
from apps.tasks.models import Job, JobEvent, JobRun
from apps.tasks.tests.test_jobs import FakeDispatcher


class JobsPageFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(
            username="admin", name="A", surname="D", auth_source=auth_source, is_superuser=True
        )
        language, _ = Language.objects.get_or_create(alpha2="sk", defaults={"name": "Slovak", "alpha3": "slk"})
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Catalog", url_name="catalog")
        cls.entry = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Sken", language=language)

        now = timezone.now()
        cls.failed_job = Job.objects.create(
            kind="files.ocr",
            entry=cls.entry,
            catalog=cls.catalog,
            state="failed",
            reason="boom",
            last_transition_at=now,
        )
        cls.ok_run = JobRun.objects.create(
            name="ocr", kind="files.ocr", job=cls.failed_job, status="succeeded", runtime=2.0, worker="w1", queue="ocr"
        )
        cls.bad_run = JobRun.objects.create(
            name="ocr",
            kind="files.ocr",
            job=cls.failed_job,
            entry=cls.entry,
            status="failed",
            runtime=4.0,
            error="boom",
            traceback="Traceback: boom",
            worker="w1",
            queue="ocr",
        )
        JobRun.objects.filter(pk=cls.ok_run.pk).update(created_at=now - timedelta(hours=3))
        JobEvent.objects.create(run=cls.bad_run, job_ref=cls.failed_job, type="task-failed", timestamp=now)

    def setUp(self):
        self.dispatcher = FakeDispatcher()
        patcher = patch.object(facade, "dispatcher", self.dispatcher)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.admin)

    def get_json(self, name, *args, **params):
        response = self.client.get(reverse(f"admin:{name}", args=args), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def post_action(self, job, action):
        return self.client.post(
            reverse("admin:console_jobs_action", args=[job.pk]),
            data=json.dumps({"action": action}),
            content_type="application/json",
        )


class JobsReadTests(JobsPageFixtures):
    def test_page_renders(self):
        response = self.client.get(reverse("admin:console_jobs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "jobsPage()")
        self.assertContains(response, "files.ocr")

    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:console_jobs_summary"))
        self.assertEqual(response.status_code, 302)

    def test_summary(self):
        data = self.get_json("console_jobs_summary")
        self.assertEqual(data["states"]["failed"], 1)
        self.assertEqual(data["states"]["running"], 0)
        self.assertEqual(data["jobs_total"], 1)
        self.assertEqual(data["runs"]["last_day"], 2)
        self.assertEqual(data["runs"]["last_hour"], 1)
        self.assertEqual(data["runs"]["failed_last_day"], 1)
        self.assertAlmostEqual(data["runs"]["failure_rate"], 0.5)
        self.assertAlmostEqual(data["runs"]["runtime_p50"], 3.0)

    def test_timeseries(self):
        data = self.get_json("console_jobs_timeseries", hours=24)
        self.assertEqual(data["bucket_minutes"], 10)
        self.assertEqual(sum(data["series"]["failed"]), 1)
        self.assertEqual(sum(data["series"]["succeeded"]), 1)
        self.assertEqual(len(data["series"]["failed"]), len(data["buckets"]))
        self.assertEqual(data["runtimes"][0]["kind"], "files.ocr")
        self.assertEqual(data["runtimes"][0]["runs"], 2)
        self.assertIn(["files.ocr", "failed", 1], data["matrix"]["cells"])

    def test_list_jobs_and_runs(self):
        jobs = self.get_json("console_jobs_list", view="jobs", state="failed")
        self.assertEqual([row["id"] for row in jobs["rows"]], [str(self.failed_job.pk)])
        self.assertEqual(jobs["rows"][0]["entry"], "Sken")
        self.assertFalse(jobs["has_next"])
        self.assertEqual(self.get_json("console_jobs_list", view="jobs", state="running")["rows"], [])

        runs = self.get_json("console_jobs_list", view="runs", state="failed")
        self.assertEqual([row["id"] for row in runs["rows"]], [str(self.bad_run.pk)])
        searched = self.get_json("console_jobs_list", view="runs", q="w1")
        self.assertEqual(len(searched["rows"]), 2)

    def test_job_and_run_detail(self):
        job = self.get_json("console_jobs_detail", self.failed_job.pk)
        self.assertEqual(job["reason"], "boom")
        self.assertEqual(len(job["runs"]), 2)
        self.assertEqual(job["events"][0]["type"], "task-failed")
        self.assertIn("retry", job["actions"])
        self.assertNotIn("cancel", job["actions"])
        failed = next(run for run in job["runs"] if run["status"] == "failed")
        self.assertEqual(failed["traceback"], "Traceback: boom")

        run = self.get_json("console_jobs_run", self.bad_run.pk)
        self.assertEqual(run["job_id"], str(self.failed_job.pk))
        self.assertEqual(run["traceback"], "Traceback: boom")
        self.assertEqual(len(run["events"]), 1)

    def test_unknown_job_is_404(self):
        response = self.client.get(reverse("admin:console_jobs_detail", args=["00000000-0000-0000-0000-000000000000"]))
        self.assertEqual(response.status_code, 404)

    def test_queues_degrade_without_broker(self):
        with patch("apps.extraction.admin_views._broker_client", return_value=None):
            data = self.get_json("console_jobs_queues")
        self.assertFalse(data["reachable"])
        ocr = next(queue for queue in data["queues"] if queue["queue"] == "ocr")
        self.assertIsNone(ocr["waiting"])

    @override_settings(EVILFLOWERS_ADMIN_MODULES="library")
    def test_module_off_is_404(self):
        self.assertEqual(self.client.get(reverse("admin:console_jobs")).status_code, 404)
        self.assertEqual(self.client.get(reverse("admin:console_jobs_summary")).status_code, 404)


class JobsActionTests(JobsPageFixtures):
    def test_cancel_and_retry_go_through_the_facade(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        self.assertEqual(job.state, "queued")

        response = self.post_action(job, "cancel")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["desired_state"], "cancelled")
        job.refresh_from_db()
        self.assertEqual(job.state, "cancelled")

        response = self.post_action(job, "retry")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["desired_state"], body["generation"]), ("completed", 2))
        self.assertEqual(len(self.dispatcher.sent), 2)

    def test_reconcile(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        response = self.post_action(job, "reconcile")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["state"], "queued")

    def test_unknown_action_is_400(self):
        self.assertEqual(self.post_action(self.failed_job, "explode").status_code, 400)

    def test_facade_refusal_is_passed_on(self):
        # A run-only kind cannot be declared: the facade answers 409, the console passes it on.
        job = Job.objects.create(kind="maintenance.unknown_kind", state="failed")
        response = self.post_action(job, "retry")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    @override_settings(EVILFLOWERS_ADMIN_READ_ONLY=True)
    def test_read_only_refuses_actions(self):
        response = self.post_action(self.failed_job, "retry")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.get_json("console_jobs_detail", self.failed_job.pk)["actions"], [])
        self.assertEqual(len(self.dispatcher.sent), 0)
