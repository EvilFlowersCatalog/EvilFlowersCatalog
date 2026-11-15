"""Reading budgets (#175): how much of a book a metered basis (`budget.bases`, e.g. `past_loan`) may read.

A per (reader, entry) counter of characters handed out within the last `window_days`, capped at
`percent_of_book` of the book's paragraph characters (its active extraction run). The counter is the sum of
`ContentDisclosure.chars_charged`, so it needs no table of its own and forgets by itself as the window moves.
Over budget the gate lowers the tier to `snippet` (`reason: budget`); a surface never cuts a passage to fit,
it leaves whole passages out and says which.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.core.cache import cache
from django.db.models import Sum
from django.db.models.functions import Length
from django.utils import timezone

from apps.core import settings_store

CACHE_TIMEOUT = 24 * 60 * 60


@dataclass(frozen=True)
class BudgetState:
    percent_of_book: float
    window_days: int
    book_chars: int
    limit: int
    used: int

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    def payload(self) -> dict:
        return {
            "percent_of_book": self.percent_of_book,
            "window_days": self.window_days,
            "book_chars": self.book_chars,
            "limit": self.limit,
            "used": self.used,
            "left": self.left,
        }


def book_chars(entry_id) -> int:
    """Characters of the paragraphs of the entry's active run (0 when it has none), cached per run."""
    from apps.extraction.models import ExtractionRun, Paragraph

    run_id = (
        ExtractionRun.objects.filter(acquisition__entry_id=entry_id, is_active=True)
        .order_by("-finished_at")
        .values_list("pk", flat=True)
        .first()
    )
    if run_id is None:
        return 0
    key = settings_store.database_cache_key(f"content-book-chars:{run_id}")
    cached = cache.get(key)
    if cached is None:
        cached = Paragraph.objects.filter(run_id=run_id).aggregate(total=Sum(Length("text")))["total"] or 0
        cache.set(key, cached, CACHE_TIMEOUT)
    return cached


def used(user_id, entry_id, window_days: int) -> int:
    from apps.content.models import ContentDisclosure

    since = timezone.now() - timedelta(days=window_days)
    return (
        ContentDisclosure.objects.filter(user_id=user_id, entry_id=entry_id, created_at__gte=since).aggregate(
            total=Sum("chars_charged")
        )["total"]
        or 0
    )


def state(user, entry, budget: dict) -> Optional[BudgetState]:
    """The budget of `user` for `entry` under the policy's `budget` document.

    None without a reader, and None for a book with no extracted paragraphs: passage-tier text comes only from an
    extraction run, so there is nothing to meter (reading whole pages needs `full`, which a metered basis that
    stays below it never reaches).
    """
    if user is None:
        return None
    total = book_chars(entry.pk)
    if not total:
        return None
    return BudgetState(
        percent_of_book=budget["percent_of_book"],
        window_days=budget["window_days"],
        book_chars=total,
        limit=int(total * budget["percent_of_book"] / 100),
        used=used(user.pk, entry.pk, budget["window_days"]),
    )


__all__ = ["BudgetState", "book_chars", "state", "used"]
