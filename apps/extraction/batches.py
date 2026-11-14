"""Job names of an extraction run, with per-batch dispatch in mind (#150).

The analyzer may declare one job per layout batch: `figures:0001-0008`, `equations:0009-0016`, …,
next to unbatched ones like `paragraphs`. A 928-page book is then ~350 jobs. Everything here
groups them by **kind**, the part before `:`, so that provenance, the stale reason and the admin
list stay as short as for an unbatched run. Pure functions, no Django.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: Distinct provenance variants kept per kind when batches disagree (a model swapped mid-run).
MAX_VARIANTS = 10
#: Kind names listed in a stale reason before the rest is summed up as "+N more".
MAX_KINDS_IN_REASON = 6

_PAGES = re.compile(r"^(\d+)-(\d+)$")


def job_kind(name: str) -> str:
    """`figures:0001-0008` → `figures`; `paragraphs` → `paragraphs`."""
    return str(name).split(":", 1)[0]


def is_batch(name: str) -> bool:
    return ":" in str(name)


def batch_pages(name: str) -> tuple[int, int] | None:
    """`figures:0001-0008` → `(1, 8)`; None for an unbatched job or a suffix that is not a page range."""
    if not is_batch(name):
        return None
    match = _PAGES.match(str(name).split(":", 1)[1])
    return (int(match[1]), int(match[2])) if match else None


@dataclass(slots=True)
class JobGroup:
    """The jobs of one kind: counts per status, and the page span of those still pending."""

    kind: str
    total: int = 0
    counts: Counter = field(default_factory=Counter)
    pending_pages: tuple[int, int] | None = None

    def add(self, name: str, status: str) -> None:
        self.total += 1
        self.counts[status] += 1
        if status == "pending" and (pages := batch_pages(name)):
            low, high = self.pending_pages or pages
            self.pending_pages = (min(low, pages[0]), max(high, pages[1]))

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "total": self.total,
            "done": self.counts["done"],
            "pending": self.counts["pending"],
            "failed": self.counts["failed"],
            "pending_pages": list(self.pending_pages) if self.pending_pages else None,
        }


def group_jobs(jobs: Mapping[str, str] | None) -> list[JobGroup]:
    """`run.jobs` grouped by kind, sorted by kind."""
    groups: dict[str, JobGroup] = {}
    for name, status in (jobs or {}).items():
        kind = job_kind(name)
        groups.setdefault(kind, JobGroup(kind)).add(name, str(status))
    return [groups[kind] for kind in sorted(groups)]


def summarise_jobs(jobs: Mapping[str, str] | None) -> dict:
    """`{"figures:0001-0008": "done", …}` → `{"figures": {"done": 115, "pending": 1}, …}`."""
    grouped: dict[str, Counter] = defaultdict(Counter)
    for name, status in (jobs or {}).items():
        grouped[job_kind(name)][str(status)] += 1
    return {kind: dict(sorted(counts.items())) for kind, counts in sorted(grouped.items())}


def pending_summary(names: Iterable[str]) -> str:
    """Pending jobs in a line: `equations ×37 (pages 297–928), figures ×2 (pages 1–16), paragraphs`.

    One job of a kind is named by its kind alone. The span covers the pending batches, gaps included.
    """
    groups = group_jobs({name: "pending" for name in names})
    if not groups:
        return "none"
    groups.sort(key=lambda group: (-group.total, group.kind))
    parts = []
    for group in groups[:MAX_KINDS_IN_REASON]:
        part = group.kind if group.total == 1 and not group.pending_pages else f"{group.kind} ×{group.total}"
        if group.pending_pages:
            low, high = group.pending_pages
            part += f" (pages {low}–{high})" if low != high else f" (page {low})"
        parts.append(part)
    if rest := groups[MAX_KINDS_IN_REASON:]:
        parts.append(f"+{len(rest)} more kinds")
    return ", ".join(parts)


def merge_provenance(provenance: Mapping | None, job: str, reported: Mapping) -> dict:
    """Record a job's provenance under its kind, so batches of one kind share one entry.

    An unbatched job replaces its entry, as before. A batch fills the kind's entry when it is
    empty; a batch reporting the same provenance changes nothing; one that differs is kept under
    the entry's `variants` (distinct, at most `MAX_VARIANTS`), so a mid-run model change is not lost.
    """
    merged = dict(provenance or {})
    reported = dict(reported)
    kind = job_kind(job)
    if not is_batch(job):
        merged[kind] = reported
        return merged
    current = merged.get(kind)
    if not isinstance(current, dict) or not current:
        merged[kind] = reported
        return merged
    base = {key: value for key, value in current.items() if key != "variants"}
    if base == reported:
        return merged
    variants = list(current.get("variants") or [])
    if reported not in variants and len(variants) < MAX_VARIANTS:
        merged[kind] = {**current, "variants": [*variants, reported]}
    return merged
