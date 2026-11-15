"""Run-independent addresses of paragraphs, sections, figures, tables and equations (#171).

A newer extraction run replaces an acquisition's paragraphs and regions (`services._finish`), and a citation that
kept a row id lost its paragraph. A locator is keyed by what the unit is, not by its row:

| Kind | Key |
|---|---|
| paragraph | `base32(sha256(entry_id, source page, normalised text))[:16]` |
| figure, table, equation | `base32(sha256(entry_id, kind, source page, normalised printed caption, quantised bbox))[:16]` |
| section | `sha1(entry_id, normalised heading, ordinal)[:20]` hex: the graph's `Section.key` (`section_key`) |

Two units with the same key in one run (a running header repeated on a page, two equal headings) get an ordinal in
reading order. An unchanged paragraph or heading keeps its key in every later run.

`assign(run)` stores the keys of a run that became active: on its paragraphs and regions, and as `Locator` rows
(created, refreshed or marked `detached_at` when the run no longer has them; never deleted). `resolve` finds a key
again in the active run, in three steps, and says which one it used:

1. `exact`: a paragraph, region or section of the active run with this key;
2. `relocated`: the stored quote (TextQuoteSelector: `exact` with `prefix`/`suffix`) found, or nearly found, in a
   unit of the same kind within ±`PAGE_WINDOW` pages: the paragraph was edited, the figure's box moved;
3. `page_only`: the page the unit was last seen on.
"""

import base64
import difflib
import hashlib
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from apps.extraction.models import ExtractionRun, Locator, Paragraph, Region

#: Characters of the quote kept on a locator.
QUOTE_CHARS = 300
#: Characters of the neighbouring text kept as `prefix` and `suffix`.
CONTEXT_CHARS = 32
#: Pages either side of the stored page a relocation searches.
PAGE_WINDOW = 2
#: A box is quantised to 1/`BBOX_GRID` of the page for a region key.
BBOX_GRID = 50
#: Least similarity (difflib ratio, 0-1) of a quote to a candidate for `relocated`.
MIN_SIMILARITY = 0.75
#: Rows per bulk write.
BATCH = 500

EXACT, RELOCATED, PAGE_ONLY = "exact", "relocated", "page_only"
REGION_KINDS = (Locator.Kind.FIGURE, Locator.Kind.TABLE, Locator.Kind.EQUATION)


def normalise(text: Optional[str]) -> str:
    """Compatibility-normalised, whitespace collapsed: what an unchanged text keeps from one run to the next."""
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def _digest(*parts) -> str:
    raw = "\x00".join(str(part) for part in parts).encode("utf-8")
    return base64.b32encode(hashlib.sha256(raw).digest()).decode("ascii").lower()[:16]


def paragraph_key(entry_id, source_page: int, text: str, ordinal: int = 0) -> str:
    parts = [entry_id, source_page, normalise(text)]
    return _digest(*(parts + [ordinal] if ordinal else parts))


def _quantised(bbox) -> Tuple[int, ...]:
    bbox = bbox or {}
    return tuple(round(float(bbox.get(name) or 0) * BBOX_GRID) for name in ("x0", "y0", "x1", "y1"))


def region_key(entry_id, kind: str, source_page: int, caption: Optional[str], bbox, ordinal: int = 0) -> str:
    parts = [entry_id, kind, source_page, normalise(caption), ",".join(map(str, _quantised(bbox)))]
    return _digest(*(parts + [ordinal] if ordinal else parts))


def section_key(entry_id, heading: str, ordinal: int = 0) -> str:
    """The graph's `Section.key`: one heading of one publication, the same in every run."""
    return hashlib.sha1(f"{entry_id}\x00{normalise(heading)}\x00{ordinal}".encode("utf-8")).hexdigest()[:20]


def section_keys(entry_id, headings: Iterable[str]) -> Dict[str, str]:
    """`{heading: key}` for an outline's headings in reading order; equal normalised headings get ordinals."""
    seen: Counter = Counter()
    keys = {}
    for heading in headings:
        if heading in keys:
            continue
        normalised = normalise(heading)
        keys[heading] = section_key(entry_id, heading, seen[normalised])
        seen[normalised] += 1
    return keys


def _caption(region) -> str:
    return region.printed_caption or ""


def _clip(text: str) -> str:
    return text[:QUOTE_CHARS]


# -- assignment ------------------------------------------------------------------------------------------------


def _paragraph_specs(entry_id, paragraphs: List[Paragraph]) -> Dict[str, dict]:
    specs: Dict[str, dict] = {}
    seen: Counter = Counter()
    texts = [normalise(paragraph.text) for paragraph in paragraphs]
    for index, paragraph in enumerate(paragraphs):
        ordinal = seen[(paragraph.page, texts[index])]
        seen[(paragraph.page, texts[index])] += 1
        key = paragraph_key(entry_id, paragraph.page, paragraph.text, ordinal)
        paragraph.locator_key, paragraph._key_changed = key, paragraph.locator_key != key
        specs[key] = {
            "kind": Locator.Kind.PARAGRAPH,
            "source_page": paragraph.page,
            "quote_exact": _clip(texts[index]),
            "prefix": texts[index - 1][-CONTEXT_CHARS:] if index else "",
            "suffix": texts[index + 1][:CONTEXT_CHARS] if index + 1 < len(texts) else "",
        }
    return specs


def _region_specs(entry_id, regions: List[Region]) -> Dict[str, dict]:
    specs: Dict[str, dict] = {}
    seen: Counter = Counter()
    for region in regions:
        caption = normalise(_caption(region))
        identity = (region.kind, region.page, caption, _quantised(region.bbox))
        ordinal = seen[identity]
        seen[identity] += 1
        key = region_key(entry_id, region.kind, region.page, caption, region.bbox, ordinal)
        region.locator_key, region._key_changed = key, region.locator_key != key
        specs[key] = {"kind": region.kind, "source_page": region.page, "quote_exact": _clip(caption)}
    return specs


def _section_specs(entry_id, run) -> Dict[str, dict]:
    from apps.extraction.outline import outline

    sections = outline(run)
    keys = section_keys(entry_id, [section.title for section in sections])
    return {
        keys[section.title]: {
            "kind": Locator.Kind.SECTION,
            "source_page": section.first_page,
            "quote_exact": _clip(normalise(section.title)),
        }
        for section in sections
    }


def assign(run: ExtractionRun) -> Dict[str, int]:
    """Key the units of a run that is (becoming) active, and bring the entry's locators up to date (idempotent).

    A locator the run has is refreshed (page, quote, `last_run`) and re-attached; one it does not have is marked
    `detached_at`, unless another active run of the entry (a second acquisition) still has it.
    """
    entry_id = run.acquisition.entry_id
    paragraphs = list(run.paragraphs.order_by("seq").only("id", "page", "seq", "text", "locator_key"))
    regions = list(
        run.regions.order_by("page", "seq").only("id", "kind", "page", "bbox", "printed_caption", "locator_key")
    )
    specs = {
        **_section_specs(entry_id, run),
        **_region_specs(entry_id, regions),
        **_paragraph_specs(entry_id, paragraphs),
    }
    now = timezone.now()

    with transaction.atomic():
        Paragraph.objects.bulk_update([p for p in paragraphs if p._key_changed], ["locator_key"], batch_size=BATCH)
        Region.objects.bulk_update([r for r in regions if r._key_changed], ["locator_key"], batch_size=BATCH)

        existing = {locator.key: locator for locator in Locator.objects.filter(entry_id=entry_id)}
        created, refreshed = [], []
        for key, spec in specs.items():
            spec = {"prefix": "", "suffix": "", **spec}
            locator = existing.get(key)
            if locator is None:
                created.append(Locator(entry_id=entry_id, key=key, first_run=run.pk, last_run=run.pk, **spec))
                continue
            changed = locator.last_run != run.pk or locator.detached_at is not None
            for name, value in spec.items():
                if getattr(locator, name) != value:
                    setattr(locator, name, value)
                    changed = True
            if changed:
                # `bulk_update` does not apply `auto_now`.
                locator.last_run, locator.detached_at, locator.updated_at = run.pk, None, now
                refreshed.append(locator)
        Locator.objects.bulk_create(created, batch_size=BATCH, ignore_conflicts=True)
        Locator.objects.bulk_update(
            refreshed,
            ["kind", "source_page", "quote_exact", "prefix", "suffix", "last_run", "detached_at", "updated_at"],
            batch_size=BATCH,
        )

        others = ExtractionRun.objects.filter(acquisition__entry_id=entry_id, is_active=True).exclude(pk=run.pk)
        stale = [key for key, locator in existing.items() if key not in specs and locator.detached_at is None]
        detached = 0
        if stale:
            detached = (
                Locator.objects.filter(entry_id=entry_id, key__in=stale)
                .exclude(last_run__in=list(others.values_list("pk", flat=True)))
                .update(detached_at=now, updated_at=now)
            )
    return {"locators": len(specs), "created": len(created), "refreshed": len(refreshed), "detached": detached}


# -- resolution ------------------------------------------------------------------------------------------------


@dataclass
class Resolution:
    locator: Locator
    #: `exact | relocated | page_only`.
    resolution: str
    #: The page of the original file the unit is on now (or was last seen on).
    source_page: int
    run: Optional[ExtractionRun] = None
    paragraph: Optional[Paragraph] = None
    region: Optional[Region] = None
    #: An outline section (`apps.extraction.outline.Section`) for a section locator.
    section: Optional[object] = None

    @property
    def bbox(self) -> Optional[dict]:
        unit = self.paragraph or self.region
        return unit.bbox if unit is not None else None


def active_run(entry_id) -> Optional[ExtractionRun]:
    """The entry's active run (newest when it has several acquisitions), as the reader views use it."""
    return (
        ExtractionRun.objects.filter(acquisition__entry_id=entry_id, is_active=True)
        .select_related("acquisition")
        .order_by("-finished_at")
        .first()
    )


def _similarity(quote: str, text: str) -> float:
    if not quote or not text:
        return 0.0
    if quote in text:
        return 1.0
    head = text[: len(quote) + CONTEXT_CHARS]
    matcher = difflib.SequenceMatcher(None, quote, head, autojunk=False)
    if matcher.real_quick_ratio() < MIN_SIMILARITY or matcher.quick_ratio() < MIN_SIMILARITY:
        return 0.0
    return matcher.ratio()


def _best(locator: Locator, candidates, text_of) -> Optional[object]:
    """The candidate whose text matches the quote best (context and page distance break ties), or None."""
    best, best_score = None, 0.0
    for candidate in candidates:
        text = normalise(text_of(candidate))
        score = _similarity(locator.quote_exact, text)
        if score < MIN_SIMILARITY:
            continue
        if locator.prefix and locator.prefix in text:
            score += 0.01
        score -= abs(candidate_page(candidate) - locator.source_page) * 0.001
        if score > best_score:
            best, best_score = candidate, score
    return best


def candidate_page(candidate) -> int:
    return getattr(candidate, "page", None) or getattr(candidate, "first_page", 0)


def _window(locator: Locator) -> Tuple[int, int]:
    return max(1, locator.source_page - PAGE_WINDOW), locator.source_page + PAGE_WINDOW


def _resolve_paragraph(locator: Locator, run: ExtractionRun) -> Optional[Resolution]:
    paragraph = run.paragraphs.filter(locator_key=locator.key).first()
    if paragraph is not None:
        return Resolution(locator, EXACT, paragraph.page, run, paragraph=paragraph)
    candidates = run.paragraphs.filter(page__range=_window(locator)).order_by("seq")
    paragraph = _best(locator, candidates, lambda p: p.text)
    if paragraph is not None:
        return Resolution(locator, RELOCATED, paragraph.page, run, paragraph=paragraph)
    return None


def _resolve_region(locator: Locator, run: ExtractionRun) -> Optional[Resolution]:
    region = run.regions.filter(locator_key=locator.key).first()
    if region is not None:
        return Resolution(locator, EXACT, region.page, run, region=region)
    candidates = list(run.regions.filter(kind=locator.kind, page__range=_window(locator)).order_by("page", "seq"))
    if locator.quote_exact:
        region = _best(locator, candidates, _caption)
    else:
        # No caption to look for: the only region of its kind left on the page (its box moved).
        same_page = [candidate for candidate in candidates if candidate.page == locator.source_page]
        region = same_page[0] if len(same_page) == 1 else None
    if region is not None:
        return Resolution(locator, RELOCATED, region.page, run, region=region)
    return None


def _resolve_section(locator: Locator, run: ExtractionRun) -> Optional[Resolution]:
    from apps.extraction.outline import outline

    sections = outline(run)
    keys = section_keys(locator.entry_id, [section.title for section in sections])
    for section in sections:
        if keys[section.title] == locator.key:
            return Resolution(locator, EXACT, section.first_page, run, section=section)
    low, high = _window(locator)
    nearby = [section for section in sections if low <= section.first_page <= high]
    section = _best(locator, nearby, lambda s: s.title)
    if section is not None:
        return Resolution(locator, RELOCATED, section.first_page, run, section=section)
    return None


def resolve(entry_id, key: str, run: Optional[ExtractionRun] = None) -> Optional[Resolution]:
    """Where `key` of `entry_id` is now (module docstring), or None for a key the entry never had."""
    locator = Locator.objects.filter(entry_id=entry_id, key=key).first()
    if locator is None:
        return None
    run = run or active_run(entry_id)
    if run is not None:
        if locator.kind == Locator.Kind.PARAGRAPH:
            found = _resolve_paragraph(locator, run)
        elif locator.kind == Locator.Kind.SECTION:
            found = _resolve_section(locator, run)
        else:
            found = _resolve_region(locator, run)
        if found is not None:
            return found
    return Resolution(locator, PAGE_ONLY, locator.source_page, run)


def keys_for(paragraph_ids: Iterable = (), region_ids: Iterable = ()) -> Dict[str, str]:
    """`{row id: locator key}` of these paragraphs and regions (rows not keyed yet are left out)."""
    keys: Dict[str, str] = {}
    paragraph_ids = [pk for pk in paragraph_ids if pk]
    region_ids = [pk for pk in region_ids if pk]
    if paragraph_ids:
        keys.update(
            (str(pk), key)
            for pk, key in Paragraph.objects.filter(pk__in=paragraph_ids, locator_key__isnull=False).values_list(
                "pk", "locator_key"
            )
        )
    if region_ids:
        keys.update(
            (str(pk), key)
            for pk, key in Region.objects.filter(pk__in=region_ids, locator_key__isnull=False).values_list(
                "pk", "locator_key"
            )
        )
    return keys


__all__ = [
    "EXACT",
    "PAGE_ONLY",
    "RELOCATED",
    "Resolution",
    "active_run",
    "assign",
    "keys_for",
    "normalise",
    "paragraph_key",
    "region_key",
    "resolve",
    "section_key",
    "section_keys",
]
