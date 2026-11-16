"""
Read events for the dashboard's `reads` series (#194).

`ReadService.record` is called next to `ActivityService.record` in the three download views, including the
anonymous open-access path that the activity history skips. Like `ActivityService.record` it never raises:
counting a read must not break a download.

What counts as one read: the first request of a file by one reader within `EVILFLOWERS_READ_DEDUP_SECONDS`.
The portal opens a book by creating a new user acquisition and downloading the whole file in one GET, so
the scope is the acquisition (not the per-open user acquisition) and a reload within the window is the same
read. pdf.js in `url` mode fetches byte ranges; the callers already skip continuation ranges
(`apps.files.ranges.is_continuation`), and the window absorbs a range that starts again from byte 0.

The reader is identified only by a keyed hash (HMAC with `SECRET_KEY`) that lives in the cache for the
window: the user id when logged in, otherwise the client address plus User-Agent (a campus NAT shares one
address, the browser string tells most of its readers apart). Nothing about the reader is stored in the table.
"""

import hashlib
import hmac
import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.core.models import ReadEvent
from apps.core.ratelimit import client_ip

logger = logging.getLogger(__name__)


def _actor(request=None, user=None) -> str:
    user = user if user is not None else getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"u:{user.pk}"
    if request is None:
        return "anonymous"
    return f"a:{client_ip(request)}|{request.META.get('HTTP_USER_AGENT', '')}"


def _dedup_key(actor: str, scope: str) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), f"{actor}|{scope}".encode(), hashlib.sha256).hexdigest()
    return f"reads:v1:{digest[:40]}"


class ReadService:
    PRUNE_BATCH_SIZE = 5000

    @staticmethod
    def record(
        entry, via: str, scope: str, request=None, user=None, authenticated: Optional[bool] = None
    ) -> Optional[ReadEvent]:
        """Count one read of `entry`, unless the same reader read the same `scope` within the window.

        `scope` names what was opened (an acquisition id, or the entry for an LCP license). `user` overrides
        `request.user` (the License Gateway is capability-token only: the reader is the license holder).
        """
        if entry is None:
            return None
        try:
            actor = _actor(request, user)
            if authenticated is None:
                authenticated = actor.startswith("u:")
            window = settings.EVILFLOWERS_READ_DEDUP_SECONDS
            if window > 0:
                try:
                    if not cache.add(_dedup_key(actor, scope), 1, window):
                        return None
                except Exception:
                    # Without the cache a read may be counted twice; that beats losing it.
                    logger.warning("reads.dedup_unavailable", extra={"event": "reads.dedup_unavailable"})
            # A savepoint: a failed insert must not poison the download's transaction.
            with transaction.atomic():
                return ReadEvent.objects.create(
                    entry_id=entry.pk, catalog_id=entry.catalog_id, via=via, authenticated=authenticated
                )
        except Exception:
            logger.exception(
                "reads.record_failed",
                extra={"event": "reads.record_failed", "via": via, "entry_id": str(entry.pk)},
            )
            return None

    @classmethod
    def prune(cls) -> int:
        """Delete events older than the retention window (never less than the 400-day statistics span)."""
        threshold = timezone.now() - timedelta(days=settings.EVILFLOWERS_READ_EVENT_RETENTION_DAYS)
        deleted = 0
        while True:
            ids = list(
                ReadEvent.objects.filter(occurred_at__lt=threshold).values_list("id", flat=True)[
                    : cls.PRUNE_BATCH_SIZE
                ]
            )
            if not ids:
                return deleted
            deleted += ReadEvent.objects.filter(pk__in=ids).delete()[0]
