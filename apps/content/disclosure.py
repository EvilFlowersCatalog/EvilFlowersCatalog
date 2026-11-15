"""Charging and logging what a surface hands out (#175).

A surface that returns text decides first (`access.decide`), then passes what it is about to return through
`record()`:

- for a metered basis (`Disclosure.metered`), whole items are kept while they fit in what is left of the budget
  and the rest are left out, never cut; the result lists what was left out;
- a row per item goes to `content_disclosure` (`user, api_key, processor, entry, locator, tier, chars,
  policy`) and the same event to the log as one JSON line (Loki), when the basis is metered or the caller is a
  machine client outside the app (an MCP or API-key caller) and the policy keeps a log
  (`log.retention_days > 0`). The in-app assistant is charged only when metered.

`prune()` deletes rows past their policy's retention, but never inside the policy's budget window.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, List, Optional, Sequence

from django.utils import timezone

from apps.content.access import Disclosure, Surface

logger = logging.getLogger("apps.content.disclosure")

#: Retention of rows whose policy no longer exists.
ORPHAN_RETENTION_DAYS = 365
#: Retention of `content_pack` rows (#176): addresses only, kept for later support checks.
PACK_RETENTION_DAYS = 90


@dataclass
class Item:
    """One unit of content about to be returned: a paragraph, a region, a page."""

    chars: int
    locator: Optional[str] = None
    payload: Any = None


@dataclass
class Charged:
    kept: List[Item] = field(default_factory=list)
    left_out: List[Item] = field(default_factory=list)
    chars_charged: int = 0
    budget_left: Optional[int] = None

    def receipt(self, decision: Disclosure) -> dict:
        receipt = decision.receipt(self.chars_charged)
        if decision.metered:
            receipt["budget_left"] = self.budget_left
        return receipt


def _machine_caller(request, surface: str) -> bool:
    return surface == Surface.MCP or getattr(request, "api_key", None) is not None


def record(
    request,
    decision: Disclosure,
    items: Sequence[Item],
    *,
    budget_left: Optional[int] = None,
    charge: bool = True,
    in_order: bool = False,
    dry_run: bool = False,
) -> Charged:
    """Keep what the budget allows, log what is handed out; `Charged` says what was kept and charged.

    `budget_left` overrides the decision's when a caller charges several times against one decision. `charge=False`
    logs without metering: snippets, which stay readable over budget. `in_order=True` is for text read in reading
    order (MCP `read`, the assistant's passages): once one item does not fit, it and everything after it are left
    out, so a reader never gets a later paragraph without the one before. Otherwise (search hits, packs) an item
    that does not fit is skipped and smaller ones after it may still be kept.
    """
    metered = decision.metered and charge
    left = budget_left if budget_left is not None else decision.budget_left
    result = Charged(budget_left=left)
    for item in items:
        if result.left_out and in_order:
            result.left_out.append(item)
            continue
        if metered and item.chars > 0 and item.chars > left:
            result.left_out.append(item)
            continue
        result.kept.append(item)
        if metered:
            left -= item.chars
            result.chars_charged += item.chars
    if decision.metered:
        result.budget_left = left

    surface = decision.surface or Surface.REST
    logged = metered or (decision.log_retention_days > 0 and _machine_caller(request, surface))
    if logged and result.kept and not dry_run:
        _write(request, decision, surface, result.kept, metered)
    return result


def _write(request, decision: Disclosure, surface: str, items: Sequence[Item], metered: bool) -> None:
    from apps.content.models import ContentDisclosure

    user = getattr(request, "user", None)
    user_id = user.pk if user is not None and user.is_authenticated else None
    api_key = getattr(request, "api_key", None)
    rows = [
        ContentDisclosure(
            user_id=user_id,
            api_key_id=getattr(api_key, "pk", None),
            processor=decision.processor,
            surface=surface,
            entry_id=decision.entry_id,
            locator=item.locator,
            tier=decision.tier,
            basis=decision.basis,
            chars=item.chars,
            chars_charged=item.chars if metered else 0,
            policy=decision.policy,
        )
        for item in items
    ]
    ContentDisclosure.objects.bulk_create(rows)
    for row in rows:
        logger.info(
            json.dumps(
                {
                    "event": "content.disclosure",
                    "user_id": str(user_id) if user_id else None,
                    "api_key_id": str(row.api_key_id) if row.api_key_id else None,
                    "processor": row.processor,
                    "surface": row.surface,
                    "entry_id": str(row.entry_id),
                    "locator": row.locator,
                    "tier": row.tier,
                    "basis": row.basis,
                    "chars": row.chars,
                    "chars_charged": row.chars_charged,
                    "policy": row.policy,
                }
            )
        )


def prune() -> int:
    """Delete disclosures past their policy's `log.retention_days`, keeping every row inside its budget window."""
    from django.db.models import Q

    from apps.content import policies
    from apps.content.models import ContentDisclosure

    now = timezone.now()
    deleted = 0
    known = Q()
    for policy in policies.all_current():
        keep_days = int(policy.document.get("log", {}).get("retention_days") or 0)
        if policy.budget:
            keep_days = max(keep_days, int(policy.budget["window_days"]))
        prefix = f"{policy.key}@"
        known |= Q(policy__startswith=prefix)
        deleted += ContentDisclosure.objects.filter(
            policy__startswith=prefix, created_at__lt=now - timedelta(days=keep_days)
        ).delete()[0]
    deleted += (
        ContentDisclosure.objects.exclude(known)
        .filter(created_at__lt=now - timedelta(days=ORPHAN_RETENTION_DAYS))
        .delete()[0]
    )
    from apps.content.models import ContentPack

    deleted += ContentPack.objects.filter(created_at__lt=now - timedelta(days=PACK_RETENTION_DAYS)).delete()[0]
    if deleted:
        logger.info("content_disclosure.prune deleted=%s", deleted)
    return deleted


__all__ = ["Charged", "Item", "prune", "record"]
