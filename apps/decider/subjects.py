"""Subject suggestions (#191): the decider files publications under the catalog's own UDC (MDT) categories.

Most publications carry one subject or none (dev, 2026-11-14: 169 `FILED_UNDER` for 164 entries), so subject
facets and the graph's subject layer are thin. The nightly sweep asks, for entries with at most one subject of
`EVILFLOWERS_DECIDER_SUBJECT_SCHEMES`, two choices over the catalog's own categories (not the generic UDC tree,
whose main class 4 is vacant): the top level, then the children of the chosen one. State: title, abstract and
table of contents.

The categories are flat rows; the hierarchy is the UDC notation itself: `621.3` falls under `621` under `62`, a
range `531/534` under `53`, `72:502/504` under `72`. A top-level category has no other category of the catalog
above it.

What happens with the answer (thresholds `subject` in `EVILFLOWERS_DECIDER_BANDS`, from calibration):

- p >= upper: filed (`entry.categories.add`, never replacing or removing anything a person filed);
- lower <= p < upper, or no thresholds yet: a review-queue row a librarian accepts or rejects;
- p < lower, or the entry already has the category: skipped.

Every answer leaves a `SubjectSuggestion` row, so an entry is asked once per model.
"""

import logging
import re
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.decider import client
from apps.decider.models import SubjectSuggestion

logger = logging.getLogger(__name__)

INSTRUCTIONS = "Which subject fits this publication best?"
#: Characters of the abstract and of the table of contents in a state.
ABSTRACT_CHARS = 3000
TOC_CHARS = 3000


def _notation(term: str) -> str:
    """The class a UDC term starts with: `502/504` -> `502`, `72:502/504` -> `72`."""
    return re.split(r"[/:+\s]", (term or "").strip(), maxsplit=1)[0]


def _above(parent, child) -> bool:
    if parent.pk == child.pk:
        return False
    upper, lower = _notation(parent.term), _notation(child.term)
    if not upper or not lower.startswith(upper):
        return False
    return len(lower) > len(upper) or len(child.term) > len(parent.term)


def tree(categories) -> Tuple[list, Dict]:
    """`(top level, {category id: direct children})` of flat UDC categories."""
    categories = sorted(categories, key=lambda category: (category.term, str(category.pk)))
    parent_of = {}
    for child in categories:
        parents = [parent for parent in categories if _above(parent, child)]
        if parents:
            parent_of[child.pk] = max(parents, key=lambda parent: (len(parent.term), parent.term))
    children = defaultdict(list)
    for child in categories:
        if child.pk in parent_of:
            children[parent_of[child.pk].pk].append(child)
    return [category for category in categories if category.pk not in parent_of], dict(children)


def name(category) -> str:
    """What the decider reads for a category: its notation and its label (English, Slovak, any), else the term."""
    label = category.label or {}
    text = label.get("en") or label.get("sk") or next((value for value in label.values() if value), "")
    return f"{category.term} {text}".strip() if text else category.term


def _state(entry, run) -> dict:
    from apps.extraction.outline import outline

    headings = [section.title for section in outline(run)] if run is not None else []
    return {
        "title": entry.title or "",
        "abstract": (entry.summary or "")[:ABSTRACT_CHARS],
        "toc": "; ".join(headings)[:TOC_CHARS],
    }


def _subject_categories(catalog):
    from apps.core.models import Category

    return list(Category.objects.filter(catalog=catalog, scheme__in=settings.EVILFLOWERS_DECIDER_SUBJECT_SCHEMES))


def latest_model() -> Optional[str]:
    return SubjectSuggestion.objects.order_by("-created_at").values_list("decider_model", flat=True).first()


def candidates(catalog, model: Optional[str], limit: int) -> list:
    """Entries of the catalog with at most one subject, not asked by `model` yet; those with none first."""
    from apps.core.models import Entry

    schemes = settings.EVILFLOWERS_DECIDER_SUBJECT_SCHEMES
    entries = Entry.objects.filter(catalog=catalog).annotate(
        subjects=Count("categories", filter=Q(categories__scheme__in=schemes), distinct=True)
    )
    entries = entries.filter(subjects__lte=1)
    if model:
        entries = entries.exclude(subject_suggestions__decider_model=model)
    return list(entries.order_by("subjects", "created_at")[:limit])


def outcome(p: Optional[float], config: Optional[dict] = None) -> str:
    config = settings.EVILFLOWERS_DECIDER_BANDS if config is None else config
    rule = (config or {}).get("subject")
    if not isinstance(rule, dict) or "upper" not in rule or "lower" not in rule or p is None:
        return SubjectSuggestion.Status.PENDING
    if p >= float(rule["upper"]):
        return SubjectSuggestion.Status.FILED
    if p >= float(rule["lower"]):
        return SubjectSuggestion.Status.PENDING
    return SubjectSuggestion.Status.SKIPPED


def _ask(states: List[dict], options: Dict[str, str]) -> Tuple[List[client.Answer], str]:
    answers, model = client.ask(states, {"category": client.choice(INSTRUCTIONS, options)})
    return [answer.get("category") or client.Answer(None, None, {}) for answer in answers], model


def _store(entry, category, p, trail, model) -> SubjectSuggestion:
    status = outcome(p)
    if category is None:
        status = SubjectSuggestion.Status.SKIPPED
    elif entry.categories.filter(pk=category.pk).exists():
        status = SubjectSuggestion.Status.SKIPPED
    with transaction.atomic():
        suggestion, _ = SubjectSuggestion.objects.update_or_create(
            entry=entry,
            decider_model=model,
            defaults={
                "catalog_id": entry.catalog_id,
                "category": category,
                "p": p,
                "trail": trail,
                "status": status,
                "decided_by": None,
                "decided_at": timezone.now() if status == SubjectSuggestion.Status.FILED else None,
            },
        )
        if status == SubjectSuggestion.Status.FILED:
            # Add, never replace: whatever a person filed stays.
            entry.categories.add(category)
    return suggestion


def suggest(catalog, entries: list) -> Tuple[List[SubjectSuggestion], str]:
    """Ask the two levels for `entries` (at most `client.BATCH`) of one catalog and store the outcome."""
    from apps.extraction.models import ExtractionRun

    top, children = tree(_subject_categories(catalog))
    if not top or not entries:
        return [], ""
    runs = {
        run.acquisition.entry_id: run
        for run in ExtractionRun.objects.filter(acquisition__entry__in=entries, is_active=True).select_related(
            "acquisition"
        )
    }
    states = [_state(entry, runs.get(entry.pk)) for entry in entries]
    by_id = {str(category.pk): category for category in top}
    for members in children.values():
        by_id.update({str(category.pk): category for category in members})

    model = ""
    first: List[Tuple[Optional[str], Optional[float], dict]] = []
    if len(top) == 1:
        first = [(str(top[0].pk), 1.0, {}) for _ in entries]
    else:
        answers, model = _ask(states, {str(category.pk): name(category) for category in top})
        first = [(answer.choice, answer.p, answer.probabilities) for answer in answers]

    picked: List[Tuple[Optional[object], Optional[float], list]] = []
    second_level = defaultdict(list)
    for index, (choice, p, probabilities) in enumerate(first):
        category = by_id.get(choice) if choice else None
        trail = [{"level": 1, "choice": choice, "p": p, "probabilities": probabilities}]
        picked.append((category, p, trail))
        if category is not None and children.get(category.pk):
            second_level[category.pk].append(index)
    for parent_id, indexes in second_level.items():
        parent = by_id[str(parent_id)]
        options = {str(parent.pk): f"{name(parent)} (general)"}
        options.update({str(child.pk): name(child) for child in children[parent_id]})
        answers, model = _ask([states[index] for index in indexes], options)
        for index, answer in zip(indexes, answers):
            category, p, trail = picked[index]
            child = by_id.get(answer.choice) if answer.choice in options else None
            trail.append({"level": 2, "choice": answer.choice, "p": answer.p, "probabilities": answer.probabilities})
            joint = p * answer.p if p is not None and answer.p is not None else None
            picked[index] = (child or category, joint, trail)

    model = model or settings.EVILFLOWERS_DECIDER_MODEL
    return [_store(entry, *picked[index], model) for index, entry in enumerate(entries)], model


def sweep(limit: Optional[int] = None, seconds: Optional[int] = None) -> dict:
    """Suggest subjects for up to `limit` entries within `seconds`, catalog by catalog."""
    from apps.core.models import Catalog

    limit = settings.EVILFLOWERS_DECIDER_SUBJECT_LIMIT if limit is None else limit
    seconds = settings.EVILFLOWERS_DECIDER_SWEEP_SECONDS if seconds is None else seconds
    started = time.monotonic()
    model = latest_model()
    statuses, failed, asked = Counter(), 0, 0
    catalogs = Catalog.objects.filter(category__scheme__in=settings.EVILFLOWERS_DECIDER_SUBJECT_SCHEMES).distinct()
    for catalog in catalogs.order_by("created_at"):
        if asked >= limit or time.monotonic() - started >= seconds:
            break
        queue = candidates(catalog, model, limit - asked)
        for position in range(0, len(queue), client.BATCH):
            if time.monotonic() - started >= seconds:
                break
            batch = queue[position : position + client.BATCH]
            try:
                stored, model = suggest(catalog, batch)
            except client.DeciderError as error:
                logger.warning("decider.subject_sweep_failed catalog=%s error=%s", catalog.url_name, error)
                failed += len(batch)
                break
            asked += len(batch)
            statuses.update(suggestion.status for suggestion in stored)
    duration = round(time.monotonic() - started, 1)
    report = {
        "asked": asked,
        "filed": statuses[SubjectSuggestion.Status.FILED],
        "queued": statuses[SubjectSuggestion.Status.PENDING],
        "skipped": statuses[SubjectSuggestion.Status.SKIPPED],
        "failed": failed,
        "model": model,
        "seconds": duration,
    }
    logger.info(
        "decider.subject_sweep asked=%s filed=%s queued=%s skipped=%s failed=%s seconds=%s model=%s",
        asked,
        report["filed"],
        report["queued"],
        report["skipped"],
        failed,
        duration,
        model,
    )
    from evil_flowers_catalog import domain_metrics

    domain_metrics.record_decider_sweep("subject", duration, statuses)
    return report


# -- the review queue -------------------------------------------------------------------------------------


def accept(suggestion: SubjectSuggestion, user) -> bool:
    """File a pending suggestion (add, never replace). False when it is not pending."""
    with transaction.atomic():
        suggestion = SubjectSuggestion.objects.select_for_update().select_related("entry").get(pk=suggestion.pk)
        if suggestion.status != SubjectSuggestion.Status.PENDING or suggestion.category_id is None:
            return False
        suggestion.entry.categories.add(suggestion.category_id)
        suggestion.status = SubjectSuggestion.Status.ACCEPTED
        suggestion.decided_by = user
        suggestion.decided_at = timezone.now()
        suggestion.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    _count_decision(SubjectSuggestion.Status.ACCEPTED)
    return True


def reject(suggestion: SubjectSuggestion, user) -> bool:
    updated = SubjectSuggestion.objects.filter(pk=suggestion.pk, status=SubjectSuggestion.Status.PENDING).update(
        status=SubjectSuggestion.Status.REJECTED, decided_by=user, decided_at=timezone.now(), updated_at=timezone.now()
    )
    if updated:
        _count_decision(SubjectSuggestion.Status.REJECTED)
    return bool(updated)


def _count_decision(status: str) -> None:
    from evil_flowers_catalog import domain_metrics

    domain_metrics.decider_answers.add(
        1, {"evilflowers.decider.task": "subject", "evilflowers.decider.answer": status}
    )
