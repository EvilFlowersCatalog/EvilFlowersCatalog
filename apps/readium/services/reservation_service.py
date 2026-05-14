"""
Reservation queue management (IP-003 Phase 3).

The queue is the head of the line for a fully-borrowed LCP-enabled `Entry`.
Users place themselves in line via `POST /readium/v1/reservations`. When an
active license terminates (return, expiry, revoke, cancel), the service
`promote_next(entry)` flips the head reservation from `queued` to
`available`, sets a `claim_deadline`, and fires the `reservation_available`
notification. The user then PATCHes `status: "claimed"` to convert into a
real license. If they miss the deadline, the Celery beat `expire_unclaimed`
sweep marks them `expired` and promotes the next user in line.

There are no client-callable "promote" or "expire" endpoints — those
transitions only happen as side effects on the server.
"""

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.models import Entry, User
from apps.readium.models import License, Reservation


class ReservationService:
    """All queue lifecycle operations live on this static class."""

    @staticmethod
    @transaction.atomic
    def enqueue(entry: Entry, user: User) -> Reservation:
        """
        Place `user` at the tail of `entry`'s queue.

        Raises:
            ValueError: if entry is not LCP-enabled, user already has an active
                license, or user already has a non-terminal reservation, or the
                user is over the per-user reservation cap.
        """
        if not entry.read_config("readium_enabled"):
            raise ValueError("Entry is not readium-enabled")

        active_license_states = [License.LicenseState.READY, License.LicenseState.ACTIVE]
        if License.objects.filter(entry=entry, user=user, state__in=active_license_states).exists():
            raise ValueError("User already has an active license for this entry")

        existing = Reservation.objects.filter(
            entry=entry,
            user=user,
            status__in=Reservation.NON_TERMINAL_STATUSES,
        ).first()
        if existing is not None:
            raise ValueError("User already has a reservation for this entry")

        cap = settings.EVILFLOWERS_READIUM_MAX_RESERVATIONS_PER_USER
        user_active = Reservation.objects.filter(user=user, status__in=Reservation.NON_TERMINAL_STATUSES).count()
        if user_active >= cap:
            raise ValueError(f"User has reached the reservation cap ({cap})")

        # Lock the queue for this entry while we compute the tail position.
        # SELECT FOR UPDATE on the existing queued rows prevents two concurrent
        # enqueues from picking the same position.
        last = (
            Reservation.objects.select_for_update()
            .filter(entry=entry, status=Reservation.Status.QUEUED)
            .order_by("-position")
            .first()
        )
        next_position = (last.position + 1) if last is not None else 1

        return Reservation.objects.create(
            entry=entry,
            user=user,
            position=next_position,
            status=Reservation.Status.QUEUED,
        )

    @staticmethod
    @transaction.atomic
    def cancel(reservation: Reservation) -> Reservation:
        """User-cancel a reservation. Only valid from queued/available. Trailing positions reflow."""
        if reservation.status in Reservation.TERMINAL_STATUSES:
            raise ValueError(f"Cannot cancel reservation in state: {reservation.status}")

        was_queued = reservation.status == Reservation.Status.QUEUED
        original_position = reservation.position

        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status", "updated_at"])

        # Re-flow positions of subsequent queued reservations on the same entry.
        if was_queued:
            Reservation.objects.filter(
                entry=reservation.entry,
                status=Reservation.Status.QUEUED,
                position__gt=original_position,
            ).update(position=F("position") - 1)

        return reservation

    @staticmethod
    @transaction.atomic
    def claim(reservation: Reservation) -> License:
        """
        Convert an `available` reservation into a real License.

        Delegates to LicenseService.create_license. On success sets
        `claimed_license_id` and status=claimed.

        Raises:
            ValueError: if reservation is not in `available` status or
                claim deadline has passed.
        """
        from .license_service import LicenseService  # avoid circular import

        if reservation.status != Reservation.Status.AVAILABLE:
            raise ValueError(f"Cannot claim reservation in state: {reservation.status}")

        if reservation.claim_deadline and timezone.now() > reservation.claim_deadline:
            # Treat as expired right now and fail the claim.
            reservation.status = Reservation.Status.EXPIRED
            reservation.save(update_fields=["status", "updated_at"])
            raise ValueError("Claim deadline has passed")

        license_obj = LicenseService.create_license(entry=reservation.entry, user=reservation.user)

        reservation.status = Reservation.Status.CLAIMED
        reservation.claimed_license = license_obj
        reservation.save(update_fields=["status", "claimed_license", "updated_at"])

        return license_obj

    @staticmethod
    @transaction.atomic
    def promote_next(entry: Entry) -> Optional[Reservation]:
        """
        If a slot is free on `entry` and the queue is non-empty, promote the head
        reservation from `queued` to `available` and set the claim deadline.

        Returns the promoted Reservation or None if nothing was promoted.

        Concurrent safety: uses SELECT FOR UPDATE on the head row so only one
        worker can promote at a time.
        """
        active_states = [License.LicenseState.READY, License.LicenseState.ACTIVE]
        now = timezone.now()

        total_slots = int(entry.read_config("readium_amount") or 0)
        active_count = License.objects.filter(entry=entry, state__in=active_states, expires_at__gt=now).count()

        # An "available" reservation also occupies a virtual slot — the user has
        # the right to claim it. Count them so we never over-promote.
        pending_promotions = Reservation.objects.filter(entry=entry, status=Reservation.Status.AVAILABLE).count()
        if active_count + pending_promotions >= total_slots:
            return None

        head = (
            Reservation.objects.select_for_update(skip_locked=True)
            .filter(entry=entry, status=Reservation.Status.QUEUED)
            .order_by("position")
            .first()
        )
        if head is None:
            return None

        claim_hours = settings.EVILFLOWERS_READIUM_RESERVATION_CLAIM_HOURS
        head.status = Reservation.Status.AVAILABLE
        head.available_at = now
        head.claim_deadline = now + timedelta(hours=claim_hours)
        head.save(update_fields=["status", "available_at", "claim_deadline", "updated_at"])

        # The promoted reservation's position no longer matters once it's
        # `available` — but we leave it untouched (matches the persisted history).
        # Re-flow trailing queue positions so position 1 is always the next queued user.
        Reservation.objects.filter(
            entry=entry,
            status=Reservation.Status.QUEUED,
            position__gt=head.position,
        ).update(position=F("position") - 1)

        return head

    @staticmethod
    @transaction.atomic
    def expire_unclaimed() -> int:
        """
        Sweep all `available` reservations whose `claim_deadline` has passed.
        For each, set status=expired and call promote_next on the entry. Returns
        the number of reservations expired.
        """
        now = timezone.now()
        expired_qs = Reservation.objects.select_for_update().filter(
            status=Reservation.Status.AVAILABLE,
            claim_deadline__lt=now,
        )
        expired_ids = list(expired_qs.values_list("id", flat=True))
        if not expired_ids:
            return 0

        # Mark expired in bulk.
        expired_rows = list(expired_qs)
        for reservation in expired_rows:
            reservation.status = Reservation.Status.EXPIRED
            reservation.save(update_fields=["status", "updated_at"])

        # Then promote next on each affected entry (deduped).
        seen_entries = set()
        for reservation in expired_rows:
            if reservation.entry_id in seen_entries:
                continue
            seen_entries.add(reservation.entry_id)
            ReservationService.promote_next(reservation.entry)

        return len(expired_rows)
