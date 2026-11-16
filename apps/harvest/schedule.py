"""When a source's own cron schedule makes it due (IP-034): read by the `apps.harvest.tasks.tick` beat task."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from celery.schedules import crontab
from django.utils import timezone


class InvalidSchedule(ValueError):
    pass


def parse(expression: str, *, now: Optional[datetime] = None) -> crontab:
    """Five-field cron, read like `evil_flowers_catalog.celery._cron`."""
    parts = (expression or "").split()
    if len(parts) != 5:
        raise InvalidSchedule("A schedule is a five-field cron expression, e.g. '0 3 * * *'.")
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    try:
        return crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
            nowfun=(lambda: now) if now is not None else None,
        )
    except (ValueError, TypeError) as error:
        raise InvalidSchedule(f"Invalid cron expression '{expression}': {error}") from error


def is_due(source, *, now: Optional[datetime] = None) -> bool:
    """Whether a firing time of the source's schedule lies after its last run (or its creation) and not after now."""
    if not source.schedule:
        return False
    now = now or timezone.now()
    last = source.last_run_at or source.created_at or now
    try:
        schedule = parse(source.schedule, now=now)
    except InvalidSchedule:
        return False
    return schedule.remaining_estimate(last) <= timedelta(0)


__all__ = ["InvalidSchedule", "is_due", "parse"]
