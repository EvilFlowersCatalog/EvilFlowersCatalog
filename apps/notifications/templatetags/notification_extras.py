"""
Template filters for notification e-mails.

Signal handlers serialise datetimes to ISO-8601 strings before handing the
context to Celery (JSON payload). Rendering those strings verbatim put
`2026-09-10T06:49:49.487565+00:00` in front of readers; these filters turn
them back into a datetime, convert to the active time zone and format them
with Django's localisation machinery.
"""

from datetime import date, datetime

from django import template
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
        parsed = timezone.localtime(parsed)
    return parsed


@register.filter
def isodatetime(value, fmt="d.m.Y H:i"):
    """`2026-09-10T06:49:49.487565+00:00` → `10.09.2026 08:49` (local time zone).

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
