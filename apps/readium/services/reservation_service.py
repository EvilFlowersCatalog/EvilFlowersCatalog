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

import logging
from datetime import timedelta
from typing import Iterable, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.models import Entry, User
from apps.readium.models import License, Reservation

logger = logging.getLogger(__name__)


def _dispatch_promoted_notifications(entry: Entry, previously_behind_positions: Iterable[int]) -> None:
    """Send `reservation_promoted` to users whose queue position decreased.

    Gated by `EVILFLOWERS_READIUM_NOTIFY_POSITION_CHANGES` (default off).
    Throttled by `EVILFLOWERS_READIUM_PROMOTED_MAX_PER_USER_PER_ENTRY_PER_DAY`
    (default 3 per 24h per (user, entry)) — protects against burst-cancellation
    storms on hot titles.

    `previously_behind_positions` is the set of *old* positions of reservations
    that just had `position -= 1` applied. After the reflow, those users sit
    at `position - 1`. We notify them.

    IP-011 Phase 3 / Q3 resolution.
    """
    if not getattr(settings, "EVILFLOWERS_READIUM_NOTIFY_POSITION_CHANGES", False):
        return

    if not getattr(settings, "EVILFLOWERS_NOTIFICATIONS_ENABLED", False):
        return

    from apps.notifications.models import NotificationLog
    from apps.notifications.tasks import send_notification

    old_positions = list(previously_behind_positions)
    if not old_positions:
        return

    cap = int(getattr(settings, "EVILFLOWERS_READIUM_PROMOTED_MAX_PER_USER_PER_ENTRY_PER_DAY", 3))
    cutoff = timezone.now() - timedelta(hours=24)

    # Re-query the rows that just reflowed — their `position` is now `old - 1`.
    affected = Reservation.objects.filter(
        entry=entry,
        status=Reservation.Status.QUEUED,
        position__in=[p - 1 for p in old_positions],
    ).select_related("user", "entry")

    cap_hits = 0
    for reservation in affected:
        sent_in_window = NotificationLog.objects.filter(
            recipient=reservation.user,
            notification_type=NotificationLog.NotificationType.RESERVATION_PROMOTED,
            created_at__gte=cutoff,
            context_snapshot__entry_id=str(entry.pk),
        ).count()
        if sent_in_window >= cap:
            cap_hits += 1
            continue

        context = {
            "user_name": reservation.user.full_name or reservation.user.username,
            "entry_id": str(entry.pk),
            "entry_title": entry.title,
            "entry_author": entry.first_author_name,
            "reservation_id": str(reservation.pk),
            "old_position": reservation.position + 1,
            "new_position": reservation.position,
        }
        try:
            send_notification.delay(
                notification_type=NotificationLog.NotificationType.RESERVATION_PROMOTED,
                recipient_user_id=str(reservation.user_id),
                context=context,
            )
        except Exception:  # pragma: no cover — best effort, never block reflow
            logger.exception(
                "Failed to enqueue reservation_promoted for user=%s entry=%s",
                reservation.user_id,
                entry.pk,
            )

    if cap_hits:
        logger.warning(
            "reservation_promoted cap hit %d times for entry=%s (per-(user,entry,24h) cap=%d)",
            cap_hits,
            entry.pk,
            cap,
        )


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
        reflowed_old_positions: list[int] = []
        if was_queued:
            reflowed_old_positions = list(
                Reservation.objects.filter(
                    entry=reservation.entry,
                    status=Reservation.Status.QUEUED,
                    position__gt=original_position,
                ).values_list("position", flat=True)
            )
            Reservation.objects.filter(
                entry=reservation.entry,
                status=Reservation.Status.QUEUED,
                position__gt=original_position,
            ).update(position=F("position") - 1)

        # IP-011 Phase 3: dispatch reservation_promoted for users that moved up
        # the queue. Best-effort — never blocks the state mutation.
        if reflowed_old_positions:
            transaction.on_commit(
                lambda entry=reservation.entry, positions=reflowed_old_positions: (
                    _dispatch_promoted_notifications(entry, positions)
                )
            )

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

        Concurrent safety (IP-008 Phase 1 A2): lock the Entry row first.
        Two parallel returns on the same entry serialize on the entry lock;
        the active-count + pending-promotion check then sees a stable view.
        Without the entry-level lock, the head reservation lock alone is
        insufficient — two workers could both observe "1 slot free" and
        promote two different heads (different rows, no row-lock conflict).
        """
        active_states = [License.LicenseState.READY, License.LicenseState.ACTIVE]
        now = timezone.now()

        # Serialize on the entry row. Other parallel promote_next callers
        # for the same entry block here.
        Entry.objects.select_for_update().get(pk=entry.pk)

        total_slots = int(entry.read_config("readium_amount") or 0)
        active_count = License.objects.filter(entry=entry, state__in=active_states, expires_at__gt=now).count()

        # An "available" reservation also occupies a virtual slot — the user has
        # the right to claim it. Count them so we never over-promote.
        pending_promotions = Reservation.objects.filter(entry=entry, status=Reservation.Status.AVAILABLE).count()
        if active_count + pending_promotions >= total_slots:
            return None

        # The entry lock already serializes us; skip_locked is not needed.
        head = (
            Reservation.objects.select_for_update()
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
        reflowed_old_positions = list(
            Reservation.objects.filter(
                entry=entry,
                status=Reservation.Status.QUEUED,
                position__gt=head.position,
            ).values_list("position", flat=True)
        )
        Reservation.objects.filter(
            entry=entry,
            status=Reservation.Status.QUEUED,
            position__gt=head.position,
        ).update(position=F("position") - 1)

        # IP-011 Phase 3: dispatch reservation_promoted for users that moved up
        # the queue. Best-effort — never blocks the state mutation.
        if reflowed_old_positions:
            transaction.on_commit(
                lambda entry=entry, positions=reflowed_old_positions: (
                    _dispatch_promoted_notifications(entry, positions)
                )
            )

        return head

    @staticmethod
    def expire_unclaimed() -> int:
        """
        Sweep all `available` reservations whose `claim_deadline` has passed.

        Two-phase (IP-008 Phase 1 A3):
        1. In ONE transaction: lock + mark expired the eligible rows,
           collect the affected entries. Commit.
        2. For each affected entry, call `promote_next` in its OWN
           transaction. This is required because `promote_next` uses
           `select_for_update` on the Entry row; nesting it under the
           sweep's outer atomic block would make the inner select see
           the outer transaction's row locks and skip / block depending
           on database. By committing the sweep first, the per-entry
           promotion runs in clean independent transactions.

        Returns the number of reservations expired.
        """
        now = timezone.now()

        with transaction.atomic():
            expired_qs = Reservation.objects.select_for_update().filter(
                status=Reservation.Status.AVAILABLE,
                claim_deadline__lt=now,
            )
            expired_rows = list(expired_qs)
            if not expired_rows:
                return 0

            affected_entry_ids: set = set()
            for reservation in expired_rows:
                reservation.status = Reservation.Status.EXPIRED
                reservation.save(update_fields=["status", "updated_at"])
                affected_entry_ids.add(reservation.entry_id)

        # Outside the sweep transaction: promote next per affected entry
        # in its own transaction. Each call serializes on the entry lock.
        for entry_id in affected_entry_ids:
            try:
                entry = Entry.objects.get(pk=entry_id)
            except Entry.DoesNotExist:
                continue
            ReservationService.promote_next(entry)

        return len(expired_rows)
