"""`GET /api/v1/statistics` — operational lending statistics for the admin dashboard (GitHub #131)."""

import logging
from datetime import date
from http import HTTPStatus
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException
from apps.core.models import Catalog, UserCatalog
from apps.core.views import SecuredView
from apps.readium.services.statistics import AdminStatistics, build_statistics, count_buckets, make_buckets
from apps.readium.services.umami import visitors_per_window

logger = logging.getLogger(__name__)

MAX_SPAN_DAYS = 400
MAX_BUCKETS = 400
CACHE_SECONDS = 60
BUCKETS = ("day", "week", "month")


def _invalid(detail: str) -> ProblemDetailException:
    return ProblemDetailException(
        _("Invalid statistics parameters"),
        status=HTTPStatus.BAD_REQUEST,
        detail_type=DetailType.VALIDATION_ERROR,
        detail=detail,
    )


def parse_params(query) -> dict:
    """Validate the query string; raises 400 problem+json on any violation."""
    try:
        catalog_id = UUID(query.get("catalog_id", ""))
    except ValueError:
        raise _invalid(_("`catalog_id` must be a UUID."))

    try:
        date_from = date.fromisoformat(query.get("from", ""))
        date_to = date.fromisoformat(query.get("to", ""))
    except ValueError:
        raise _invalid(_("`from` and `to` must be dates in YYYY-MM-DD format."))
    if date_from > date_to:
        raise _invalid(_("`from` must not be after `to`."))
    if (date_to - date_from).days + 1 > MAX_SPAN_DAYS:
        raise _invalid(_("The period must not be longer than %(days)d days.") % {"days": MAX_SPAN_DAYS})

    bucket = query.get("bucket") or "day"
    if bucket not in BUCKETS:
        raise _invalid(_("`bucket` must be one of day, week, month."))
    if count_buckets(date_from, date_to, bucket) > MAX_BUCKETS:
        raise _invalid(_("The period must not span more than %(n)d buckets.") % {"n": MAX_BUCKETS})

    tz = query.get("tz") or settings.TIME_ZONE
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise _invalid(_("`tz` must be an IANA time zone."))

    return {"catalog_id": catalog_id, "date_from": date_from, "date_to": date_to, "bucket": bucket, "tz_name": tz}


def _overlay_visitors(data: AdminStatistics, params: dict) -> None:
    """#194: portal-wide Umami visitors per bucket. Never fails the response: `null` on any trouble."""
    try:
        buckets = make_buckets(params["date_from"], params["date_to"], params["bucket"], ZoneInfo(params["tz_name"]))
        counts = visitors_per_window([(b.window.start, b.window.end) for b in buckets], timezone.now())
    except Exception:
        logger.exception("statistics.visitors_failed", extra={"event": "statistics.visitors_failed"})
        return
    for row, count in zip(data.series, counts):
        row.visitors = count


class StatisticsView(SecuredView):
    @openapi.metadata(
        description=(
            "Aggregated lending statistics of one catalog for the admin dashboard: what needs attention now, "
            "KPIs compared with the preceding period of equal length, zero-filled calendar-aligned series, the "
            "weekday × hour heatmap of loans, per-title copy utilization, holds ratio and turnover, hold and loan "
            "outcomes and the top categories. Query: `catalog_id`, `from` and `to` (inclusive local dates, at most "
            "400 days), `bucket` (`day`, `week`, `month`; at most 400 buckets) and `tz` (IANA zone, default the "
            "server zone). Superusers and catalog managers only. Aggregates only, never per-reader data. "
            "Each series bucket also carries `reads` (publications opened or downloaded, any access path, anonymous "
            "included, de-duplicated per reader and file within 30 minutes) and `visitors` (distinct portal "
            "visitors from Umami's website stats, **portal-wide** even with `catalog_id`; `null` when analytics is "
            "not configured, unreachable or slower than 2 s). `null` means not available, `0` means measured, "
            "nobody. Cached for 60 seconds."
        ),
        tags=["Statistics"],
        summary="Catalog lending statistics",
    )
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to read statistics"))

        params = parse_params(request.GET)
        catalog_id = params["catalog_id"]

        if not Catalog.objects.filter(pk=catalog_id).exists():
            raise ProblemDetailException(
                _("Catalog not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
            )
        if (
            not request.user.is_superuser
            and not UserCatalog.objects.filter(
                user=request.user, catalog_id=catalog_id, mode=UserCatalog.Mode.MANAGE
            ).exists()
        ):
            raise ProblemDetailException(
                _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN, detail_type=DetailType.FORBIDDEN
            )

        # Access is checked above; the numbers do not depend on who asks, so the key has no user.
        # The language is part of it because category titles are localized.
        language = get_language()
        # v2: buckets carry `reads` and `visitors` (#194).
        key = "statistics:v2:{catalog_id}:{date_from}:{date_to}:{bucket}:{tz_name}".format(**params)
        key = f"{key}:{language}"
        cached = cache.get(key)
        if cached is not None:
            data = AdminStatistics.model_validate(cached)
        else:
            data = build_statistics(**params, language=language)
            _overlay_visitors(data, params)
            cache.set(key, data.model_dump(by_alias=True), CACHE_SECONDS)

        return SingleResponse(request, data=data)
