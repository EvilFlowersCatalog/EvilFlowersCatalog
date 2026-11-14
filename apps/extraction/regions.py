"""Figures and tables as search documents (IP-030 §3).

A figure's searchable text is its printed caption, the first paragraph that refers to it
(`extraction_reference`) and its model description once there is one. It is embedded with the same e5 model
as paragraphs (`tei-embed`, IP-021) into `extraction_region.embedding`, with the model recorded, so
`search_regions` never compares vectors of two models. Equations are left out unless they have a caption: a
LaTeX string is a poor search document.

`search_regions` works like `discovery.search_passages`: access control first (and only publications whose
text the reader may read outside the viewer: a figure's caption and crop are its content), then meaning and
words, fused by reciprocal rank, cut at `EVILFLOWERS_SEARCH_REGION_MIN_SCORE`.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from django.conf import settings
from django.db import connection, transaction
from pgvector import HalfVector
from pgvector.django import CosineDistance

from apps.extraction.models import ExtractionRun, Region

logger = logging.getLogger(__name__)

#: Characters of the referring paragraph in a figure's document.
CONTEXT_CHARS = 600
#: Characters of a document in total (e5 takes 512 tokens).
DOCUMENT_CHARS = 1500
#: A caption shorter than this many words says nothing alone ("Figure 3").
MIN_CAPTION_WORDS = 4

#: Candidates ranked per channel before fusion.
CANDIDATES = 100


def document_text(region: Region, context: Optional[str]) -> Optional[str]:
    """The text a region is searched by, or None when there is nothing to search."""
    caption = " ".join((region.printed_caption or region.caption or "").split())
    description = " ".join((region.description or "").split())
    context = " ".join((context or "").split())[:CONTEXT_CHARS]
    if region.kind == Region.Kind.EQUATION and not caption:
        return None
    parts = []
    if caption:
        parts.append(caption)
    if description and description != caption:
        parts.append(description)
    if context:
        parts.append(context)
    if not parts or (len(caption.split()) < MIN_CAPTION_WORDS and not (description or context)):
        return None
    return "\n".join(parts)[:DOCUMENT_CHARS]


def index_run(run: ExtractionRun, *, embed: bool = True) -> Dict[str, int]:
    """Set the document text of the run's regions and embed the ones that changed. Returns counts."""
    from apps.core.search import embeddings
    from apps.extraction.references import referring_paragraphs

    regions = list(run.regions.all())
    context = referring_paragraphs([region.pk for region in regions])
    changed: List[Region] = []
    counts = {"regions": len(regions), "documents": 0, "embedded": 0}
    for region in regions:
        paragraph = context.get(str(region.pk))
        text = document_text(region, paragraph.text if paragraph else None)
        if text:
            counts["documents"] += 1
        if text != region.embedding_text:
            region.embedding_text = text
            region.embedding = None
            region.embedding_model = None
            changed.append(region)
    if changed:
        Region.objects.bulk_update(changed, ["embedding_text", "embedding", "embedding_model"], batch_size=500)

    if not embed or not embeddings.enabled():
        return counts
    pending = [region for region in regions if region.embedding_text and region.embedding is None]
    if not pending:
        return counts
    model = embeddings.model_id(timeout=embeddings.BATCH_TIMEOUT)
    vectors = embeddings.embed_documents([region.embedding_text for region in pending])
    for region, vector in zip(pending, vectors):
        region.embedding = vector
        region.embedding_model = model
    Region.objects.bulk_update(pending, ["embedding", "embedding_model"], batch_size=500)
    counts["embedded"] = len(pending)
    return counts


@dataclass
class FigureHit:
    region_id: str
    entry_id: str
    kind: str
    page: int
    caption: Optional[str]
    score: float
    has_image: bool


def search_regions(
    request,
    text: str,
    *,
    kinds=(Region.Kind.FIGURE, Region.Kind.TABLE),
    limit: int = 3,
    scope=None,
    vector: Optional[List[float]] = None,
    min_score: Optional[float] = None,
) -> List[FigureHit]:
    """Figures (and tables) about `text` in publications the caller may see and read, best first."""
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Entry
    from apps.core.search import embeddings
    from apps.core.search.query import rrf
    from apps.extraction.discovery import RRF_K, _document, _may_read_text, _tsquery

    text = (text or "").strip()[:2000]
    if not text:
        return []
    visible = EntryFilter(scope if scope is not None else {}, queryset=Entry.objects.all(), request=request).qs
    # Processed publications the reader may see (a few hundred), then only those readable here.
    runs = dict(
        ExtractionRun.objects.filter(is_active=True, acquisition__entry__in=visible.values("pk")).values_list(
            "pk", "acquisition__entry_id"
        )
    )
    readable = {
        entry.pk
        for entry in Entry.objects.filter(pk__in=set(runs.values())).select_related("catalog")
        if _may_read_text(request, entry)
    }
    runs = {run_id: entry_id for run_id, entry_id in runs.items() if entry_id in readable}
    if not runs:
        return []
    candidates = Region.objects.filter(run_id__in=list(runs), kind__in=list(kinds), embedding_text__isnull=False)

    if vector is None:
        vector = embeddings.try_embed_query(text, "passages")
    by_meaning: Dict = {}
    if vector is not None:
        model = embeddings.known_model_id()
        floor = settings.EVILFLOWERS_SEARCH_REGION_MIN_SCORE if min_score is None else min_score
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cursor.execute(f"SET LOCAL hnsw.ef_search = {CANDIDATES}")
            rows = (
                candidates.filter(embedding__isnull=False, embedding_model=model)
                .annotate(distance=CosineDistance("embedding", HalfVector(vector)))
                .order_by("distance")
                .values_list("pk", "distance")[:CANDIDATES]
            )
            by_meaning = {pk: 1 - distance for pk, distance in rows if 1 - distance >= floor}

    document = _document(f'"{Region._meta.db_table}"."embedding_text"')
    by_words = list(
        candidates.extra(where=[f"{document} @@ {_tsquery()}"], params=[text]).values_list("pk", flat=True)[
            :CANDIDATES
        ]
    )
    fused = [pk for pk, _score in rrf([sorted(by_meaning, key=lambda pk: -by_meaning[pk]), by_words], k=RRF_K)]
    if not fused:
        return []
    regions = {region.pk: region for region in Region.objects.filter(pk__in=fused[: limit * 3])}
    hits = []
    for pk in fused:
        region = regions.get(pk)
        if region is None:
            continue
        hits.append(
            FigureHit(
                region_id=str(region.pk),
                entry_id=str(runs[region.run_id]),
                kind=region.kind,
                page=region.page,
                caption=region.printed_caption or region.caption or region.description,
                score=round(by_meaning.get(pk, 0.0), 4),
                has_image=bool(region.image_path),
            )
        )
        if len(hits) >= limit:
            break
    return hits
