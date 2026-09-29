"""
Personal activity history (IP-016).

`ActivityService.record` is called from download/shelf/share views and from the Readium
lifecycle signals. It collapses a repeated action into the user's latest row for the entry and
never raises: a history write must not break a download or a loan.
"""

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from apps.core.models import UserActivity

logger = logging.getLogger(__name__)


class ActivityService:
    PRUNE_BATCH_SIZE = 5000

    @staticmethod
    def record(user, entry, action: str, metadata: Optional[dict] = None) -> None:
        if user is None or not user.is_authenticated or entry is None:
            return

        metadata = metadata or {}

        try:
            now = timezone.now()
            latest = (
                UserActivity.objects.filter(user=user, entry=entry)
                .order_by("-last_occurred_at")
                .only("id", "action")
                .first()
            )

            if latest and latest.action == action:
                UserActivity.objects.filter(pk=latest.pk).update(
                    count=F("count") + 1, last_occurred_at=now, metadata=metadata, updated_at=now
                )
            else:
                UserActivity.objects.create(
                    user=user, entry=entry, action=action, last_occurred_at=now, metadata=metadata
                )
        except Exception:
            logger.exception(
                "activity.record_failed",
                extra={"event": "activity.record_failed", "action": action, "entry_id": str(entry.pk)},
            )

    @classmethod
    def prune(cls) -> int:
        """Delete rows whose latest occurrence is older than the retention window."""
        threshold = timezone.now() - timedelta(days=settings.EVILFLOWERS_ACTIVITY_RETENTION_DAYS)
        deleted = 0

        while True:
            ids = list(
                UserActivity.objects.filter(last_occurred_at__lt=threshold).values_list("id", flat=True)[
                    : cls.PRUNE_BATCH_SIZE
                ]
            )
            if not ids:
                return deleted
            deleted += UserActivity.objects.filter(pk__in=ids).delete()[0]
