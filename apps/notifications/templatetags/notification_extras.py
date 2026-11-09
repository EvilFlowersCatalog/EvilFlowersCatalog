"""
Template filters for notification e-mails.

Signal handlers serialise datetimes to ISO-8601 strings before handing the
context to Celery (JSON payload). Rendering those strings verbatim put
`2026-09-10T06:49:49.487565+00:00` in front of readers; these filters turn
them back into a datetime, convert to EVILFLOWERS_DISPLAY_TIME_ZONE and
format them with Django's localisation machinery. The processes run in UTC
(#107), so the reader's zone is applied here, not inherited from TIME_ZONE.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django import template
from django.conf import settings
from django.utils import formats, timezone
from django.utils.dateparse import parse_datetime

register = template.Library()


def _to_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str) and value.strip():
        parsed = parse_datetime(value.strip())
        if parsed is None:
            return None
    else:
        return None
    if timezone.is_aware(parsed):
        parsed = timezone.localtime(parsed, ZoneInfo(settings.EVILFLOWERS_DISPLAY_TIME_ZONE))
    return parsed


@register.filter
def isodatetime(value, fmt="d.m.Y H:i"):
    """`2026-09-10T06:49:49.487565+00:00` → `10.09.2026 08:49` (display time zone).

    Numeric day-month-year reads the same for Slovak and English recipients;
    Django's locale `DATETIME_FORMAT` would print "midnight"/"noon" in English.
    """
    parsed = _to_datetime(value)
    if parsed is None:
        return value or ""
    return formats.date_format(parsed, fmt)


@register.filter
def isodate(value):
    """Date-only variant of `isodatetime`."""
    return isodatetime(value, "d.m.Y")
