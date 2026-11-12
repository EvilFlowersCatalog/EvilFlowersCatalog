"""
Operational lending statistics for the admin dashboard (GitHub #131).

`build_statistics` answers `GET /api/v1/statistics` for one catalog in a fixed
number of queries, independent of how many titles or loans the catalog holds:

1. LCP titles with point-in-time copy usage, last loan and lendability,
2. their authors (only for the titles returned),
3. every license that matters for the period or the previous one,
4. every reservation that matters for them, plus all non-terminal ones,
5. encryption jobs that need attention,
6. the top categories by loans.

Licenses and reservations are folded in Python. Interval metrics (copy
utilization, active readers) are overlaps with each window, which grouping by
a truncated timestamp cannot produce, and one pass over the rows serves the
period, the previous period, every bucket and every title at once.

Only aggregates leave this module: no user ids, names or per-reader figures.
"""

from __future__ import annotations

import bisect
import statistics as pystats
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

from django.db.models import Count, Exists, IntegerField, Max, OuterRef, Q, Subquery, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

from apps.core.fields.localized import localize
from apps.core.models import Category, Entry, EntryAuthor
from apps.readium.models import EncryptedContent, License, Reservation

StatsBucket = Literal["day", "week", "month"]

#: `titles[]` is capped; `titles_truncated` reports the cut.
TITLES_LIMIT = 1000
#: `categories[]` length.
CATEGORIES_LIMIT = 10
#: A pending encryption job whose last dispatch (or creation) is older than this is stuck.
ENCRYPTION_STUCK_AFTER = timedelta(hours=1)
#: An available reservation whose claim deadline falls within this window needs attention.
CLAIM_EXPIRY_HORIZON = timedelta(hours=24)

DAY_SECONDS = 86400.0

#: Terminal states whose real end is the moment of the transition. Returning moves `expires_at`
#: to the return time (`StatusServerSync.return_license`), but only when the Status Server says so
#: or the optimistic fallback runs; revoking and cancelling never move it. `min(expires_at,
#: updated_at)` is right in every case.
_ENDED_EARLY = (License.LicenseState.RETURNED, License.LicenseState.REVOKED, License.LicenseState.CANCELLED)
_LIVE = (License.LicenseState.READY, License.LicenseState.ACTIVE)
_RESOLVED = (Reservation.Status.CLAIMED, Reservation.Status.EXPIRED, Reservation.Status.CANCELLED)


# ---------------------------------------------------------------- response models (portal contract)


class Kpi(BaseModel):
    value: Optional[float]
    previous: Optional[float]
    trend: List[Optional[float]]


class StatisticsPeriod(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # `from` is a Python keyword; responses are dumped `by_alias`.
    from_: str = Field(alias="from")
    to: str
    previous_from: str
    previous_to: str
    bucket: StatsBucket
    tz: str


class StatisticsAttention(BaseModel):
    encryption_failed: int
    encryption_stuck: int
    lcp_not_lendable: int
    claims_expiring_24h: int
    saturated_with_queue: int


class StatisticsKpis(BaseModel):
    loans: Kpi
    active_readers: Kpi
    utilization: Kpi
    hold_fulfillment: Kpi
    median_wait_hours: Kpi
    copies_in_use_now: int
    copies_total: int
    lcp_titles: int


class StatisticsBucket(BaseModel):
    t: str
    partial: bool
    loans_started: int
    loans_returned: int
    loans_expired: int
    holds_placed: int


class StatisticsHeatCell(BaseModel):
    dow: int
    hour: int
    loans: int


class TitleStatistics(BaseModel):
    entry_id: str
    title: str
    authors: List[str]
    copies: int
    in_use: int
    queued: int
    loans: int
    loan_days: float
    utilization: float
    holds_ratio: float
    turnover: float
    avg_loan_days: Optional[float]
    added_at: str
    last_loan_at: Optional[str]


class HoldOutcomes(BaseModel):
    placed: int
    claimed: int
    expired_unclaimed: int
    cancelled: int
    still_waiting: int


class LoanOutcomes(BaseModel):
    returned_early: int
    ran_to_expiry: int
    revoked: int
    cancelled: int
    still_active: int


class CategoryStatistics(BaseModel):
    category_id: str
    title: str
    loans: int


class AdminStatistics(BaseModel):
    generated_at: str
    period: StatisticsPeriod
    attention: StatisticsAttention
    kpis: StatisticsKpis
    series: List[StatisticsBucket]
    heatmap: List[StatisticsHeatCell]
    titles: List[TitleStatistics]
    titles_truncated: bool
    hold_outcomes: HoldOutcomes
    loan_outcomes: LoanOutcomes
    categories: List[CategoryStatistics]


# ---------------------------------------------------------------- windows and buckets


@dataclass(frozen=True)
class Window:
    """Half-open `[start, end)` in absolute time."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    def contains(self, moment: Optional[datetime]) -> bool:
        return moment is not None and self.start <= moment < self.end

    def overlap_seconds(self, start: datetime, end: datetime) -> float:
        return max(0.0, (min(end, self.end) - max(start, self.start)).total_seconds())


def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def align(day: date, bucket: StatsBucket) -> date:
    """Calendar-aligned bucket start containing `day` (ISO week from Monday; first of month)."""
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def next_bucket(day: date, bucket: StatsBucket) -> date:
    if bucket == "week":
        return day + timedelta(days=7)
    if bucket == "month":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day + timedelta(days=1)


def bucket_starts(date_from: date, date_to: date, bucket: StatsBucket) -> List[date]:
    starts, current = [], align(date_from, bucket)
    while current <= date_to:
        starts.append(current)
        current = next_bucket(current, bucket)
    return starts


def count_buckets(date_from: date, date_to: date, bucket: StatsBucket) -> int:
    return len(bucket_starts(date_from, date_to, bucket))


@dataclass(frozen=True)
class Bucket:
    start: date
    partial: bool
    window: Window  # clipped to the period


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return numerator / denominator if denominator else None


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment is not None else None


# ---------------------------------------------------------------- queries


def lcp_entries(catalog_id):
    """LCP titles: `readium_enabled` and `readium_amount >= 1`, with `read_config` defaults (false, 1)."""
    return (
        Entry.objects.filter(catalog_id=catalog_id, config__readium_enabled=True)
        .annotate(
            copies=Coalesce(Cast(KeyTextTransform("readium_amount", "config"), IntegerField()), Value(1)),
        )
        .filter(copies__gte=1)
    )


def _loan_end(state: str, expires_at: datetime, updated_at: datetime) -> datetime:
    return min(expires_at, updated_at) if state in _ENDED_EARLY else expires_at


@dataclass
class _TitleAcc:
    loans: int = 0
    loan_seconds: float = 0.0
    ended_lengths: List[float] = field(default_factory=list)


def build_statistics(
    catalog_id,
    date_from: date,
    date_to: date,
    bucket: StatsBucket,
    tz_name: str,
    language: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AdminStatistics:
    tz = ZoneInfo(tz_name)
    now = now or timezone.now()

    days = (date_to - date_from).days + 1
    previous_to = date_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=days - 1)

    period = Window(local_midnight(date_from, tz), local_midnight(date_to + timedelta(days=1), tz))
    previous = Window(local_midnight(previous_from, tz), period.start)
    span = Window(previous.start, period.end)

    buckets: List[Bucket] = []
    for start in bucket_starts(date_from, date_to, bucket):
        end = next_bucket(start, bucket)
        buckets.append(
            Bucket(
                start=start,
                partial=start < date_from or end - timedelta(days=1) > date_to,
                window=Window(
                    local_midnight(max(start, date_from), tz),
                    local_midnight(min(end, date_to + timedelta(days=1)), tz),
                ),
            )
        )
    bucket_edges = [b.window.start for b in buckets]

    def bucket_of(moment: datetime) -> Optional[int]:
        if not period.contains(moment):
            return None
        return bisect.bisect_right(bucket_edges, moment) - 1

    # --- 1. titles (point in time)
    live_licenses = License.objects.filter(entry=OuterRef("pk"), state__in=_LIVE, expires_at__gt=now)
    title_rows = list(
        lcp_entries(catalog_id)
        .annotate(
            licenses_in_use=Coalesce(
                Subquery(live_licenses.order_by().values("entry").annotate(c=Count("*")).values("c")[:1]),
                Value(0),
            ),
            last_loan_at=Subquery(
                License.objects.filter(entry=OuterRef("pk"))
                .order_by()
                .values("entry")
                .annotate(m=Max("created_at"))
                .values("m")[:1]
            ),
            lendable=Exists(
                EncryptedContent.objects.filter(
                    acquisition__entry=OuterRef("pk"), status=EncryptedContent.EncryptionStatus.REGISTERED
                )
            ),
        )
        .values("id", "title", "created_at", "copies", "licenses_in_use", "last_loan_at", "lendable")
    )
    lcp_ids = {row["id"] for row in title_rows}

    # --- 3. licenses touching the period or the previous one
    license_rows = list(
        License.objects.filter(entry__catalog_id=catalog_id)
        .filter(
            Q(created_at__gte=span.start, created_at__lt=span.end)
            | Q(starts_at__lt=span.end, expires_at__gt=span.start)
        )
        .values_list("entry_id", "user_id", "state", "created_at", "starts_at", "expires_at", "updated_at")
    )

    # --- 4. reservations touching the windows, plus every live one (queues, claims)
    reservation_rows = list(
        Reservation.objects.filter(entry__catalog_id=catalog_id)
        .filter(
            Q(requested_at__gte=span.start, requested_at__lt=span.end)
            | Q(available_at__gte=span.start, available_at__lt=span.end)
            | Q(status__in=_RESOLVED, updated_at__gte=span.start, updated_at__lt=span.end)
            | Q(status__in=Reservation.NON_TERMINAL_STATUSES)
        )
        .values_list("entry_id", "status", "requested_at", "available_at", "claim_deadline", "updated_at")
    )

    # --- fold licenses
    n = len(buckets)
    loans = {"cur": 0, "prev": 0}
    readers = {"cur": set(), "prev": set()}
    lcp_seconds = {"cur": 0.0, "prev": 0.0}
    b_started, b_returned, b_expired = [0] * n, [0] * n, [0] * n
    b_readers = [set() for _ in range(n)]
    b_seconds = [0.0] * n
    heat = [[0] * 24 for _ in range(7)]
    outcomes = defaultdict(int)
    per_title: Dict = defaultdict(_TitleAcc)

    for entry_id, user_id, state, created_at, starts_at, expires_at, updated_at in license_rows:
        end = _loan_end(state, expires_at, updated_at)
        is_lcp = entry_id in lcp_ids

        for key, window in (("cur", period), ("prev", previous)):
            if window.contains(created_at):
                loans[key] += 1
            if starts_at < window.end and end > window.start:
                readers[key].add(user_id)
            if is_lcp:
                lcp_seconds[key] += window.overlap_seconds(starts_at, end)

        if period.contains(created_at):
            local = created_at.astimezone(tz)
            heat[local.weekday()][local.hour] += 1
            outcomes[state] += 1
            b_started[bucket_of(created_at)] += 1
            if is_lcp:
                per_title[entry_id].loans += 1

        if state == License.LicenseState.RETURNED and (i := bucket_of(end)) is not None:
            b_returned[i] += 1
        if state == License.LicenseState.EXPIRED and (i := bucket_of(expires_at)) is not None:
            b_expired[i] += 1

        if starts_at < period.end and end > period.start:
            first = max(0, bisect.bisect_right(bucket_edges, starts_at) - 1)
            for i in range(first, n):
                window = buckets[i].window
                if window.start >= end:
                    break
                seconds = window.overlap_seconds(starts_at, end)
                if seconds > 0:
                    b_readers[i].add(user_id)
                    if is_lcp:
                        b_seconds[i] += seconds
            if is_lcp:
                per_title[entry_id].loan_seconds += period.overlap_seconds(starts_at, end)

        if is_lcp and period.contains(end) and end <= now:
            per_title[entry_id].ended_lengths.append((end - starts_at).total_seconds() / DAY_SECONDS)

    # --- fold reservations
    queued_by_entry, available_by_entry = defaultdict(int), defaultdict(int)
    claims_expiring = 0
    resolved = {"cur": defaultdict(int), "prev": defaultdict(int)}
    waits = {"cur": [], "prev": []}
    b_holds = [0] * n
    b_resolved = [defaultdict(int) for _ in range(n)]
    b_waits = [[] for _ in range(n)]
    holds = defaultdict(int)

    for entry_id, status, requested_at, available_at, claim_deadline, updated_at in reservation_rows:
        if status == Reservation.Status.QUEUED:
            queued_by_entry[entry_id] += 1
        elif status == Reservation.Status.AVAILABLE:
            available_by_entry[entry_id] += 1
            if claim_deadline is not None and claim_deadline < now + CLAIM_EXPIRY_HORIZON:
                claims_expiring += 1

        wait = (available_at - requested_at).total_seconds() / 3600 if available_at is not None else None
        for key, window in (("cur", period), ("prev", previous)):
            if status in _RESOLVED and window.contains(updated_at):
                resolved[key][status] += 1
            if wait is not None and window.contains(available_at):
                waits[key].append(wait)

        if period.contains(requested_at):
            holds[status] += 1
            b_holds[bucket_of(requested_at)] += 1
        if status in _RESOLVED and (i := bucket_of(updated_at)) is not None:
            b_resolved[i][status] += 1
        if wait is not None and (i := bucket_of(available_at)) is not None:
            b_waits[i].append(wait)

    def fulfillment(counts) -> Optional[float]:
        return _ratio(counts[Reservation.Status.CLAIMED], sum(counts[s] for s in _RESOLVED))

    def median(values) -> Optional[float]:
        return pystats.median(values) if values else None

    # --- titles
    copies_total = sum(row["copies"] for row in title_rows)
    period_days = period.seconds / DAY_SECONDS
    titles: List[TitleStatistics] = []
    in_use_now = saturated = not_lendable = 0
    for row in title_rows:
        entry_id, copies = row["id"], row["copies"]
        in_use = row["licenses_in_use"] + available_by_entry[entry_id]
        queued = queued_by_entry[entry_id]
        acc = per_title.get(entry_id) or _TitleAcc()
        loan_days = acc.loan_seconds / DAY_SECONDS
        in_use_now += in_use
        saturated += int(in_use >= copies and queued > 0)
        not_lendable += int(not row["lendable"])
        titles.append(
            TitleStatistics(
                entry_id=str(entry_id),
                title=row["title"],
                authors=[],
                copies=copies,
                in_use=in_use,
                queued=queued,
                loans=acc.loans,
                loan_days=round(loan_days, 2),
                utilization=round(loan_days / (copies * period_days), 4),
                holds_ratio=round(queued / copies, 4),
                turnover=round(acc.loans / copies, 4),
                avg_loan_days=_round(pystats.mean(acc.ended_lengths), 2) if acc.ended_lengths else None,
                added_at=row["created_at"].isoformat(),
                last_loan_at=_iso(row["last_loan_at"]),
            )
        )
    titles.sort(key=lambda t: (-t.utilization, -t.loans, t.title))
    titles_truncated = len(titles) > TITLES_LIMIT
    titles = titles[:TITLES_LIMIT]

    # --- 2. authors of the returned titles
    authors: Dict[str, List[str]] = defaultdict(list)
    for entry_id, name, surname in (
        EntryAuthor.objects.filter(entry_id__in=[t.entry_id for t in titles])
        .order_by("entry_id", "position")
        .values_list("entry_id", "author__name", "author__surname")
    ):
        authors[str(entry_id)].append(f"{name} {surname}".strip())
    for title in titles:
        title.authors = authors.get(title.entry_id, [])

    # --- 5. encryption attention
    encryption = EncryptedContent.objects.filter(acquisition__entry__catalog_id=catalog_id).aggregate(
        failed=Count("pk", filter=Q(status=EncryptedContent.EncryptionStatus.FAILED)),
        stuck=Count(
            "pk",
            filter=Q(status=EncryptedContent.EncryptionStatus.PENDING)
            & (
                Q(last_dispatched_at__lt=now - ENCRYPTION_STUCK_AFTER)
                | Q(last_dispatched_at__isnull=True, created_at__lt=now - ENCRYPTION_STUCK_AFTER)
            ),
        ),
    )

    # --- 6. categories
    category_rows = (
        Category.objects.filter(catalog_id=catalog_id)
        .annotate(
            loans=Count(
                "entries__licenses",
                filter=Q(
                    entries__licenses__created_at__gte=period.start,
                    entries__licenses__created_at__lt=period.end,
                ),
            )
        )
        .filter(loans__gt=0)
        .order_by("-loans", "term")
        .values("id", "term", "label", "loans")[:CATEGORIES_LIMIT]
    )

    # --- KPIs
    def utilization(seconds: float, window: Window) -> Optional[float]:
        return _ratio(seconds, copies_total * window.seconds)

    kpis = StatisticsKpis(
        loans=Kpi(value=loans["cur"], previous=loans["prev"], trend=list(b_started)),
        active_readers=Kpi(
            value=len(readers["cur"]),
            previous=len(readers["prev"]),
            trend=[len(r) for r in b_readers],
        ),
        utilization=Kpi(
            value=_round(utilization(lcp_seconds["cur"], period)),
            previous=_round(utilization(lcp_seconds["prev"], previous)),
            trend=[_round(utilization(b_seconds[i], b.window)) for i, b in enumerate(buckets)],
        ),
        hold_fulfillment=Kpi(
            value=_round(fulfillment(resolved["cur"])),
            previous=_round(fulfillment(resolved["prev"])),
            trend=[_round(fulfillment(c)) for c in b_resolved],
        ),
        median_wait_hours=Kpi(
            value=_round(median(waits["cur"]), 2),
            previous=_round(median(waits["prev"]), 2),
            trend=[_round(median(w), 2) for w in b_waits],
        ),
        copies_in_use_now=in_use_now,
        copies_total=copies_total,
        lcp_titles=len(title_rows),
    )

    S, LS = Reservation.Status, License.LicenseState
    return AdminStatistics(
        generated_at=now.isoformat(),
        period=StatisticsPeriod(
            from_=date_from.isoformat(),
            to=date_to.isoformat(),
            previous_from=previous_from.isoformat(),
            previous_to=previous_to.isoformat(),
            bucket=bucket,
            tz=tz_name,
        ),
        attention=StatisticsAttention(
            encryption_failed=encryption["failed"],
            encryption_stuck=encryption["stuck"],
            lcp_not_lendable=not_lendable,
            claims_expiring_24h=claims_expiring,
            saturated_with_queue=saturated,
        ),
        kpis=kpis,
        series=[
            StatisticsBucket(
                t=b.start.isoformat(),
                partial=b.partial,
                loans_started=b_started[i],
                loans_returned=b_returned[i],
                loans_expired=b_expired[i],
                holds_placed=b_holds[i],
            )
            for i, b in enumerate(buckets)
        ],
        heatmap=[StatisticsHeatCell(dow=d, hour=h, loans=heat[d][h]) for d in range(7) for h in range(24)],
        titles=titles,
        titles_truncated=titles_truncated,
        hold_outcomes=HoldOutcomes(
            placed=sum(holds.values()),
            claimed=holds[S.CLAIMED],
            expired_unclaimed=holds[S.EXPIRED],
            cancelled=holds[S.CANCELLED],
            still_waiting=holds[S.QUEUED] + holds[S.AVAILABLE],
        ),
        loan_outcomes=LoanOutcomes(
            returned_early=outcomes[LS.RETURNED],
            ran_to_expiry=outcomes[LS.EXPIRED],
            revoked=outcomes[LS.REVOKED],
            cancelled=outcomes[LS.CANCELLED],
            still_active=outcomes[LS.READY] + outcomes[LS.ACTIVE],
        ),
        categories=[
            CategoryStatistics(
                category_id=str(row["id"]),
                title=localize(row["label"], language) or row["term"],
                loans=row["loans"],
            )
            for row in category_rows
        ],
    )
