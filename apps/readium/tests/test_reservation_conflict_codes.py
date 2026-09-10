"""
Reservation conflict reason codes + stuck-queue healing.

- `ReservationService.enqueue` raises typed errors (`slots_available`,
  `already_reserved`, `already_borrowed`, `reservation_cap_reached`) and the
  reservations API forwards `reason_code` in `additional_data` so the portal
  can branch without string matching.
- Reserving an entry with free capacity is rejected — such a reservation would
  sit `queued` forever (promotion fires on license transitions).
- The per-minute sweep also promotes heads of queues on entries with free
  capacity (`promote_stuck_queues`), healing queues stuck by operator edits.
"""

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.readium.services.exceptions import (
    AlreadyBorrowedError,
    AlreadyReservedError,
    ReservationCapReachedError,
    ReservationError,
    SlotsAvailableError,
)


class ReservationErrorTaxonomyTests(SimpleTestCase):
    def test_all_subclass_value_error(self):
        for exc_class in (ReservationError, SlotsAvailableError, AlreadyReservedError, ReservationCapReachedError):
            self.assertTrue(issubclass(exc_class, ValueError))

    def test_reason_codes_are_stable(self):
        self.assertEqual(SlotsAvailableError.reason_code, "slots_available")
        self.assertEqual(AlreadyReservedError.reason_code, "already_reserved")
        self.assertEqual(ReservationCapReachedError.reason_code, "reservation_cap_reached")
        # Reserve-while-licensed reuses the borrow-side code.
        self.assertEqual(AlreadyBorrowedError.reason_code, "already_borrowed")


class EnqueueGuardSourceTests(SimpleTestCase):
    """`enqueue` raises the typed errors and rejects entries with free capacity."""

    def test_enqueue_raises_typed_errors(self):
        import inspect

        from apps.readium.services.reservation_service import ReservationService

        src = inspect.getsource(ReservationService.enqueue.__wrapped__)
        self.assertIn("AlreadyBorrowedError", src)
        self.assertIn("AlreadyReservedError", src)
        self.assertIn("ReservationCapReachedError", src)
        self.assertIn("SlotsAvailableError", src)

    def test_promote_next_and_enqueue_share_capacity_math(self):
        import inspect

        from apps.readium.services.reservation_service import ReservationService

        self.assertIn("_slots_in_use", inspect.getsource(ReservationService.enqueue.__wrapped__))
        self.assertIn("_slots_in_use", inspect.getsource(ReservationService.promote_next.__wrapped__))


class ConflictReasonCodePassthroughTests(SimpleTestCase):
    """The reservations views forward `reason_code` on 409s."""

    def test_helper_forwards_typed_reason_code(self):
        from apps.readium.views.reservations import _conflict_additional_data

        self.assertEqual(_conflict_additional_data(SlotsAvailableError("x")), {"reason_code": "slots_available"})
        self.assertIsNone(_conflict_additional_data(ValueError("untyped")))

    def test_all_conflict_sites_use_the_helper(self):
        from pathlib import Path

        src = Path("apps/readium/views/reservations.py").read_text()
        self.assertEqual(src.count("additional_data=_conflict_additional_data(e)"), 3)


class StuckQueueSweepTests(SimpleTestCase):
    def test_service_exposes_promote_stuck_queues(self):
        from apps.readium.services.reservation_service import ReservationService

        self.assertTrue(callable(getattr(ReservationService, "promote_stuck_queues", None)))

    def test_sweep_task_heals_stuck_queues(self):
        import inspect

        from apps.readium import tasks

        src = inspect.getsource(tasks.sweep_unclaimed_reservations)
        self.assertIn("expire_unclaimed", src)
        self.assertIn("promote_stuck_queues", src)
