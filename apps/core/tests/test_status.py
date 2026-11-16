"""#195: `/api/v1/status` checks the dependencies every runtime has; supervisord only where it runs."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.api.views import status


def ok():
    return None


def broken():
    raise ConnectionRefusedError("broker down")


ALL_OK = {"database": status.check_database, "broker": ok, "storage": ok}


@override_settings(EVILFLOWERS_STATUS_SUPERVISOR=False)
class StatusWithoutSupervisordTests(TestCase):
    def test_k8s_answers_200_with_the_dependency_checks(self):
        with mock.patch.dict(status.CHECKS, ALL_OK, clear=True):
            response = self.client.get(reverse("api:status"))

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()["response"]
        self.assertEqual(body["checks"], {"database": "ok", "broker": "ok", "storage": "ok"})
        self.assertIn("stats", body)

    def test_supervisord_is_not_asked(self):
        with (
            mock.patch.dict(status.CHECKS, ALL_OK, clear=True),
            mock.patch.object(status, "supervisord_processes") as supervisord,
        ):
            self.client.get(reverse("api:status"))
        supervisord.assert_not_called()

    def test_a_failing_dependency_is_503_and_named(self):
        with mock.patch.dict(status.CHECKS, {**ALL_OK, "broker": broken}, clear=True):
            response = self.client.get(reverse("api:status"))

        self.assertEqual(response.status_code, 503)
        data = response.json()["additional_data"]
        self.assertEqual(data["database"], "ok")
        # The class only: the message may name hosts and users, and the endpoint is anonymous.
        self.assertEqual(data["broker"], "failed: ConnectionRefusedError")

    def test_the_storage_check_lists_the_storage_root(self):
        status.check_storage()


@override_settings(EVILFLOWERS_STATUS_SUPERVISOR=True)
class StatusWithSupervisordTests(TestCase):
    def test_unreachable_supervisord_is_still_503(self):
        with (
            mock.patch.dict(status.CHECKS, ALL_OK, clear=True),
            mock.patch.object(status, "supervisord_processes", side_effect=FileNotFoundError()),
        ):
            response = self.client.get(reverse("api:status"))
        self.assertEqual(response.status_code, 503)

    def test_a_stopped_process_is_503(self):
        with (
            mock.patch.dict(status.CHECKS, ALL_OK, clear=True),
            mock.patch.object(status, "supervisord_processes", return_value={"gunicorn": "RUNNING", "beat": "FATAL"}),
        ):
            response = self.client.get(reverse("api:status"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["additional_data"]["beat"], "FATAL")

    def test_all_running_is_200(self):
        with (
            mock.patch.dict(status.CHECKS, ALL_OK, clear=True),
            mock.patch.object(status, "supervisord_processes", return_value={"gunicorn": "RUNNING"}),
        ):
            response = self.client.get(reverse("api:status"))
        self.assertEqual(response.status_code, 200)
