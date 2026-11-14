"""`library_overview`: what the library holds, without a model (IP-030 §4).

"What are the publications mostly about?" took 9 rounds and 6 minutes on 2026-11-12. This answers it in one
call from the catalog itself, over the publications the reader may see: how many there are and how many are
processed (searchable inside), their languages and years, the subjects with the most publications and the
most-read titles of each, and — from the AGE graph — the concepts that span the most subjects. Nothing in it is
generated, so there is nothing for the model to invent.
"""

from collections import Counter
from typing import Dict, List

from django.db.models import Count

#: Subjects listed, and titles per subject.
SUBJECTS = 12
TITLES_PER_SUBJECT = 3
CONCEPTS = 10

SPECIFICATION = (
    "The whole library: size, languages, years, main subjects with most-read titles, recurring concepts.",
    {"type": "object", "properties": {}},
)


def _label(category, language: str) -> str:
    label = category.label if isinstance(category.label, dict) else {}
    return label.get(language) or label.get("en") or label.get("sk") or category.term


def overview(context, arguments: dict) -> dict:
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Category, Entry
    from apps.extraction.models import ExtractionRun

    request = context.request
    language = (context.language or "en").split("-")[0]
    visible = EntryFilter({}, queryset=Entry.objects.all(), request=request).qs.distinct()
    ids = list(visible.values_list("pk", flat=True))
    processed = (
        ExtractionRun.objects.filter(is_active=True, acquisition__entry_id__in=ids)
        .values("acquisition__entry_id")
        .distinct()
        .count()
    )
    languages = Counter(
        code for code in Entry.objects.filter(pk__in=ids).values_list("language__alpha2", flat=True) if code
    )
    years = sorted(
        int(str(value)[:4])
        for value in Entry.objects.filter(pk__in=ids, published_at__isnull=False).values_list(
            "published_at", flat=True
        )
        if str(value)[:4].isdigit()
    )

    categories = (
        Category.objects.filter(entries__in=ids)
        .annotate(publications=Count("entries", distinct=True))
        .order_by("-publications", "term")[:SUBJECTS]
    )
    subjects: List[Dict] = []
    for category in categories:
        titles = list(
            Entry.objects.filter(pk__in=ids, categories=category)
            .order_by("-popularity", "title")
            .values_list("title", flat=True)[:TITLES_PER_SUBJECT]
        )
        subjects.append(
            {
                "term": category.term,
                "label": _label(category, language),
                "publications": category.publications,
                "most_read": titles,
            }
        )

    payload = {
        "publications": len(ids),
        "processed": processed,
        "languages": [{"code": code, "publications": count} for code, count in languages.most_common(6)],
        "years": {"from": years[0], "to": years[-1]} if years else None,
        "subjects": subjects,
    }
    concepts = _concepts([str(pk) for pk in ids])
    if concepts:
        payload["concepts"] = concepts
    payload["note"] = "Counts are over the publications this reader may see. 'processed' ones can be read and cited."
    return payload


def _concepts(entry_ids: List[str]) -> List[dict]:
    try:
        from apps.graph import queries

        return queries.top_concepts(entry_ids, CONCEPTS)
    except Exception:
        return []
