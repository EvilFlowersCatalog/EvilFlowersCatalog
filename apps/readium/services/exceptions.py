"""
Typed domain exceptions for the readium service layer.

`BorrowError` subclasses carry a machine-readable `reason_code` so the API
layer can translate borrow-availability conflicts into structured problem
details (HTTP 409 + `reason_code` in `additional_data`) without string-matching
exception messages. The frontend uses `reason_code == "no_available_slots"`
to offer the reservation queue instead of showing a generic validation error.

Every class subclasses `ValueError` so pre-existing `except ValueError`
call sites (reservation claim view, claim page, management commands) keep
working unchanged.
"""

from typing import Optional


class BorrowError(ValueError):
    """Base class for borrow-availability failures raised by `LicenseService`."""

    reason_code = "borrow_conflict"

    def __init__(self, message: str, availability: Optional[dict] = None):
        super().__init__(message)
        self.availability = availability or {}


class NotReadiumEnabledError(BorrowError):
    reason_code = "not_readium_enabled"


class AlreadyBorrowedError(BorrowError):
    reason_code = "already_borrowed"


class NoAvailableSlotsError(BorrowError):
    reason_code = "no_available_slots"


_BORROW_ERRORS_BY_REASON_CODE = {
    exc.reason_code: exc for exc in (NotReadiumEnabledError, AlreadyBorrowedError, NoAvailableSlotsError)
}


def borrow_error_from_availability(availability: dict) -> BorrowError:
    """Build the typed exception matching a `LicenseService.can_user_borrow` result."""
    exc_class = _BORROW_ERRORS_BY_REASON_CODE.get(availability.get("reason_code"), BorrowError)
    return exc_class(f"Cannot create license: {availability.get('reason', 'unavailable')}", availability)


__all__ = [
    "BorrowError",
    "NotReadiumEnabledError",
    "AlreadyBorrowedError",
    "NoAvailableSlotsError",
    "borrow_error_from_availability",
]
