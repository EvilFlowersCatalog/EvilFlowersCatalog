"""
Borrow-conflict contract regression tests.

`POST /readium/v1/licenses` on a fully-borrowed entry must answer 409 CONFLICT
with a machine-readable `reason_code` in `additional_data` — the frontend uses
`no_available_slots` to offer the reservation queue instead of rendering a
generic validation error. Availability failures are raised as typed
`BorrowError` subclasses from the service layer so no string matching happens
in the view.
"""

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.readium.services.exceptions import (
    AlreadyBorrowedError,
    BorrowError,
    NoAvailableSlotsError,
    NotReadiumEnabledError,
    borrow_error_from_availability,
)


class BorrowErrorTaxonomyTests(SimpleTestCase):
    """Typed exceptions stay ValueError-compatible and carry stable reason codes."""

    def test_all_subclass_value_error(self):
        for exc_class in (BorrowError, NotReadiumEnabledError, AlreadyBorrowedError, NoAvailableSlotsError):
            self.assertTrue(issubclass(exc_class, ValueError))

    def test_reason_codes_are_stable(self):
        self.assertEqual(NotReadiumEnabledError.reason_code, "not_readium_enabled")
        self.assertEqual(AlreadyBorrowedError.reason_code, "already_borrowed")
        self.assertEqual(NoAvailableSlotsError.reason_code, "no_available_slots")

    def test_factory_maps_reason_code_to_subclass(self):
        availability = {
            "can_borrow": False,
            "reason": "No available slots for the requested period",
            "reason_code": "no_available_slots",
        }
        error = borrow_error_from_availability(availability)
        self.assertIsInstance(error, NoAvailableSlotsError)
        self.assertEqual(error.availability, availability)
        self.assertIn("No available slots", str(error))

    def test_factory_falls_back_to_base_class(self):
        error = borrow_error_from_availability({"can_borrow": False, "reason": "something new"})
        self.assertIs(type(error), BorrowError)
        self.assertEqual(error.reason_code, "borrow_conflict")


class CanUserBorrowReasonCodeTests(SimpleTestCase):
    """`can_user_borrow` failure dicts carry the reason_code the factory dispatches on."""

    def test_source_declares_all_reason_codes(self):
        import inspect

        from apps.readium.services.license_service import LicenseService

        src = inspect.getsource(LicenseService.can_user_borrow)
        for reason_code in ("not_readium_enabled", "already_borrowed", "no_available_slots"):
            self.assertIn(f'"reason_code": "{reason_code}"', src)

    def test_create_license_raises_typed_error(self):
        import inspect

        from apps.readium.services.license_service import LicenseService

        src = inspect.getsource(LicenseService.create_license)
        self.assertIn("borrow_error_from_availability", src)


class BorrowConflictViewContractTests(SimpleTestCase):
    """The borrow endpoint maps typed availability errors to structured responses."""

    def test_view_maps_borrow_error_to_conflict(self):
        from pathlib import Path

        src = Path("apps/readium/views/licenses.py").read_text()
        self.assertIn("except BorrowError", src)
        self.assertIn("except NotReadiumEnabledError", src)
        # Conflict branch answers 409 and ships the structured payload.
        self.assertIn("_borrow_conflict_data", src)

    def test_conflict_payload_for_no_available_slots(self):
        from unittest.mock import patch

        from apps.readium.views.licenses import LicenseManagement

        entry = MagicMock()
        entry.pk = "11111111-1111-1111-1111-111111111111"
        request = MagicMock()

        error = NoAvailableSlotsError("Cannot create license: No available slots for the requested period")
        state = {
            entry.pk: {
                "queue_length": 3,
                "next_available_at": None,
                "user_reservation_id": None,
            }
        }
        with patch("apps.readium.views.licenses.lcp_state_mapping", return_value=state):
            data = LicenseManagement._borrow_conflict_data(request, entry, error)

        self.assertEqual(data["reason_code"], "no_available_slots")
        self.assertEqual(data["entry_id"], entry.pk)
        self.assertEqual(data["queue_length"], 3)
        self.assertIsNone(data["next_available_at"])
        self.assertIsNone(data["user_reservation_id"])
        self.assertEqual(data["reservations_url"], "/readium/v1/reservations")

    def test_conflict_payload_for_already_borrowed(self):
        from apps.readium.views.licenses import LicenseManagement

        entry = MagicMock()
        entry.pk = "11111111-1111-1111-1111-111111111111"
        request = MagicMock()

        error = AlreadyBorrowedError(
            "Cannot create license: User already has an active license for this entry",
            availability={"existing_license": "22222222-2222-2222-2222-222222222222"},
        )
        data = LicenseManagement._borrow_conflict_data(request, entry, error)

        self.assertEqual(data["reason_code"], "already_borrowed")
        self.assertEqual(data["existing_license_id"], "22222222-2222-2222-2222-222222222222")
