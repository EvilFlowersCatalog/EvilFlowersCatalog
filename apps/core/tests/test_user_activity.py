"""
IP-016 — personal activity history.

Surface-level (SimpleTestCase + mocks), following the readium test style.

Covers:

- `ActivityService.record` collapses a repeat of the user's latest action on an entry,
  inserts otherwise, skips anonymous users and never raises.
- Readium signals map License / Reservation transitions to history actions and stay silent
  when the state did not change.
- The `/api/v1/activity` route is registered and the retention task is scheduled.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from apps.core.models import UserActivity

Action = UserActivity.ActivityAction


def _user():
    user = MagicMock()
    user.is_authenticated = True
    return user


def _entry():
    entry = MagicMock()
    entry.pk = uuid4()
    return entry


class RecordCollapseTests(SimpleTestCase):
    def _objects(self, latest_action=None):
        objects = MagicMock()
        latest = None
        if latest_action is not None:
            latest = MagicMock(pk=uuid4(), action=latest_action)
        objects.filter.return_value.order_by.return_value.only.return_value.first.return_value = latest
        return objects, latest

    def test_same_action_increments_latest_row(self):
        from apps.core.services.activity import ActivityService

        objects, latest = self._objects(Action.ENTRY_DOWNLOADED)
        with patch.object(UserActivity, "objects", objects):
            ActivityService.record(_user(), _entry(), Action.ENTRY_DOWNLOADED, {"mime": "application/pdf"})

        objects.create.assert_not_called()
        objects.filter.assert_any_call(pk=latest.pk)
        update_kwargs = objects.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs["metadata"], {"mime": "application/pdf"})
        self.assertIn("count", update_kwargs)

    def test_different_action_inserts_new_row(self):
        from apps.core.services.activity import ActivityService

        objects, _ = self._objects(Action.SHELF_ADDED)
        with patch.object(UserActivity, "objects", objects):
            ActivityService.record(_user(), _entry(), Action.ENTRY_DOWNLOADED)

        objects.create.assert_called_once()
        self.assertEqual(objects.create.call_args.kwargs["action"], Action.ENTRY_DOWNLOADED)
        self.assertEqual(objects.create.call_args.kwargs["metadata"], {})

    def test_first_activity_for_entry_inserts(self):
        from apps.core.services.activity import ActivityService

        objects, _ = self._objects(None)
        with patch.object(UserActivity, "objects", objects):
            ActivityService.record(_user(), _entry(), Action.LOAN_CREATED)

        objects.create.assert_called_once()

    def test_anonymous_user_is_skipped(self):
        from apps.core.services.activity import ActivityService

        anonymous = MagicMock(is_authenticated=False)
        objects, _ = self._objects(None)
        with patch.object(UserActivity, "objects", objects):
            ActivityService.record(anonymous, _entry(), Action.ENTRY_DOWNLOADED)
            ActivityService.record(None, _entry(), Action.ENTRY_DOWNLOADED)

        objects.filter.assert_not_called()

    def test_database_error_is_swallowed(self):
        from apps.core.services.activity import ActivityService

        objects = MagicMock()
        objects.filter.side_effect = RuntimeError("db down")
        with patch.object(UserActivity, "objects", objects):
            ActivityService.record(_user(), _entry(), Action.ENTRY_DOWNLOADED)  # must not raise


class ReadiumSignalTests(SimpleTestCase):
    def _license(self, state, original_state):
        return MagicMock(pk=uuid4(), state=state, _original_state=original_state, expires_at=timezone.now())

    def _reservation(self, status, original_status, claim_deadline=None):
        return MagicMock(pk=uuid4(), status=status, _original_status=original_status, claim_deadline=claim_deadline)

    @patch("apps.readium.signals.ActivityService.record")
    def test_license_created_records_loan_created(self, record):
        from apps.readium.models import License
        from apps.readium.signals import _record_license_activity

        _record_license_activity(License, self._license(License.LicenseState.READY, None), created=True)
        self.assertEqual(record.call_args.args[2], Action.LOAN_CREATED)

    @patch("apps.readium.signals.ActivityService.record")
    def test_license_transitions(self, record):
        from apps.readium.models import License
        from apps.readium.signals import _record_license_activity

        cases = {
            License.LicenseState.RETURNED: Action.LOAN_RETURNED,
            License.LicenseState.EXPIRED: Action.LOAN_EXPIRED,
            License.LicenseState.REVOKED: Action.LOAN_REVOKED,
            License.LicenseState.CANCELLED: Action.LOAN_CANCELLED,
        }
        for state, action in cases.items():
            record.reset_mock()
            _record_license_activity(License, self._license(state, License.LicenseState.ACTIVE), created=False)
            self.assertEqual(record.call_args.args[2], action, state)

    @patch("apps.readium.signals.ActivityService.record")
    def test_license_unchanged_or_activated_is_silent(self, record):
        from apps.readium.models import License
        from apps.readium.signals import _record_license_activity

        active = License.LicenseState.ACTIVE
        _record_license_activity(License, self._license(active, active), created=False)
        _record_license_activity(License, self._license(active, License.LicenseState.READY), created=False)
        record.assert_not_called()

    @patch("apps.readium.signals.ActivityService.record")
    def test_reservation_available_carries_claim_deadline(self, record):
        from apps.readium.models import Reservation
        from apps.readium.signals import _record_reservation_activity

        deadline = timezone.now()
        instance = self._reservation(Reservation.Status.AVAILABLE, Reservation.Status.QUEUED, deadline)
        _record_reservation_activity(Reservation, instance, created=False)

        self.assertEqual(record.call_args.args[2], Action.RESERVATION_AVAILABLE)
        self.assertEqual(record.call_args.args[3]["claim_deadline"], deadline.isoformat())

    @patch("apps.readium.signals.ActivityService.record")
    def test_reservation_created_and_transitions(self, record):
        from apps.readium.models import Reservation
        from apps.readium.signals import _record_reservation_activity

        _record_reservation_activity(Reservation, self._reservation(Reservation.Status.QUEUED, None), created=True)
        self.assertEqual(record.call_args.args[2], Action.RESERVATION_CREATED)

        cases = {
            Reservation.Status.CLAIMED: Action.RESERVATION_CLAIMED,
            Reservation.Status.EXPIRED: Action.RESERVATION_EXPIRED,
            Reservation.Status.CANCELLED: Action.RESERVATION_CANCELLED,
        }
        for status, action in cases.items():
            record.reset_mock()
            instance = self._reservation(status, Reservation.Status.AVAILABLE)
            _record_reservation_activity(Reservation, instance, created=False)
            self.assertEqual(record.call_args.args[2], action, status)


class WiringTests(SimpleTestCase):
    def test_activity_route_registered(self):
        from django.urls import reverse

        self.assertEqual(reverse("api:user-activity-management"), "/api/v1/activity")

    def test_prune_task_scheduled(self):
        from evil_flowers_catalog.celery import app

        self.assertEqual(
            app.conf.beat_schedule["core-prune-user-activity"]["task"], "apps.tasks.tasks.prune_user_activity"
        )
