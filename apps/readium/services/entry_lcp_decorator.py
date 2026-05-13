"""
Compute per-(entry, user) LCP availability state in a single batch query and
return a `dict[entry_id, dict]` that the entry serializer reads via
`info.context["lcp_states"]` — the same pattern `shelf_record_mapping` uses
for `shelf_record_id`.

Public API:

    lcp_state_mapping(user, entry_ids) -> dict[UUID, dict]
        Returns a mapping `entry_id -> {lcp_state, available_slots, total_slots,
        next_available_at, user_active_license_id, queue_length,
        user_reservation_id, user_position}`. Entries not in the mapping
        serialize with the `not_lcp` defaults.

Reservation lookups are guarded so this module works before Phase 3 migrates.
"""

from typing import Iterable, Optional
from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from apps.api.serializers.entries import LcpState
from apps.core.models import Entry
from apps.readium.models import License


def lcp_state_mapping(user, entries: Iterable) -> dict:
    """
    Build a `{entry_id: lcp_state_dict}` mapping for the given entries.

    Pass either an iterable of `Entry` instances or a queryset — we extract
    primary keys and the `config` JSON in one pass. The result is suitable for
    `serializer_context={"lcp_states": ...}`.
    """
    entries_list = list(entries) if not isinstance(entries, QuerySet) else list(entries)
    if not entries_list:
        return {}

    if user is not None and not getattr(user, "is_authenticated", False):
        user = None

    now = timezone.now()
    active_states = [License.LicenseState.READY, License.LicenseState.ACTIVE]
    entry_ids = [e.pk for e in entries_list]

    # One round-trip for all active licenses on the relevant entries.
    license_rows = License.objects.filter(
        entry_id__in=entry_ids,
        state__in=active_states,
        expires_at__gt=now,
    ).values("id", "entry_id", "user_id", "expires_at")

    licenses_by_entry: dict[UUID, list] = {}
    for row in license_rows:
        licenses_by_entry.setdefault(row["entry_id"], []).append(row)

    # Reservation queue (Phase 3 — guarded).
    reservations_by_entry: dict[UUID, list] = {}
    Reservation = _import_reservation_model()
    if Reservation is not None:
        res_rows = Reservation.objects.filter(
            entry_id__in=entry_ids,
            status__in=[Reservation.Status.QUEUED, Reservation.Status.AVAILABLE],
        ).values("id", "entry_id", "user_id", "status", "position")
        for row in res_rows:
            reservations_by_entry.setdefault(row["entry_id"], []).append(row)

    result: dict[UUID, dict] = {}
    for entry in entries_list:
        result[entry.pk] = _resolve_one(
            entry,
            user,
            licenses_by_entry.get(entry.pk, []),
            reservations_by_entry.get(entry.pk, []),
            Reservation,
        )
    return result


def _import_reservation_model():
    try:
        from apps.readium.models import Reservation

        return Reservation
    except (ImportError, AttributeError):
        return None


def _resolve_one(entry: Entry, user, license_rows: list, reservation_rows: list, Reservation) -> dict:
    if not entry.read_config("readium_enabled"):
        return {
            "lcp_state": LcpState.NOT_LCP,
            "available_slots": 0,
            "total_slots": 0,
            "next_available_at": None,
            "user_active_license_id": None,
            "queue_length": 0,
            "user_reservation_id": None,
            "user_position": None,
        }

    total_slots = int(entry.read_config("readium_amount") or 0)
    active_count = len(license_rows)
    available_slots = max(0, total_slots - active_count)

    user_license_id: Optional[UUID] = None
    if user is not None:
        for row in license_rows:
            if row["user_id"] == user.pk:
                user_license_id = row["id"]
                break

    next_available_at = None
    if available_slots == 0 and license_rows:
        next_available_at = min(row["expires_at"] for row in license_rows)

    queue_length = len(reservation_rows)
    user_reservation_id: Optional[UUID] = None
    user_position: Optional[int] = None
    if user is not None and reservation_rows and Reservation is not None:
        for row in reservation_rows:
            if row["user_id"] == user.pk:
                user_reservation_id = row["id"]
                if row["status"] == Reservation.Status.QUEUED:
                    user_position = row["position"]
                break

    if user_license_id is not None:
        state = LcpState.ACTIVE_LOAN_FOR_USER
    elif available_slots > 0:
        state = LcpState.AVAILABLE_NOW
    elif next_available_at is not None:
        state = LcpState.AVAILABLE_IN_DAYS
    else:
        state = LcpState.FULLY_BORROWED

    return {
        "lcp_state": state,
        "available_slots": available_slots,
        "total_slots": total_slots,
        "next_available_at": next_available_at,
        "user_active_license_id": user_license_id,
        "queue_length": queue_length,
        "user_reservation_id": user_reservation_id,
        "user_position": user_position,
    }
