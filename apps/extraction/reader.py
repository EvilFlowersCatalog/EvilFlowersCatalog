"""Extraction output seen through a reader's copy (IP-018 Phases 5 and 6b).

Everything here speaks the reader's page numbers, given as the copy's
`source_pages` (`apps.assistant.pdf.reader_source_pages`): what the viewer
shows. Pages outside the copy's range are never returned.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pgvector.django import CosineDistance

from apps.extraction.models import ExtractionRun, Paragraph, ParagraphLink, Region, Sentence
from apps.extraction.services import active_run

logger = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    """text-service could not embed the request in time."""


@dataclass
class Candidate:
    paragraph: Paragraph
    score: float
    #: The reader's page of the paragraph.
    page: int
    sentences: List = field(default_factory=list)


class ReaderExtraction:
    def __init__(self, acquisition_id, source_pages: List[Optional[int]], run: Optional[ExtractionRun] = None):
        self.run = run if run is not None else active_run(acquisition_id)
        self.source_pages = source_pages
        self._reader_page = {source: index + 1 for index, source in enumerate(source_pages) if source}

    @classmethod
    def for_document(cls, document) -> Optional["ReaderExtraction"]:
        """For an `apps.assistant.pdf.ReaderDocument`; None without an active run."""
        if document is None or document.acquisition is None:
            return None
        extraction = cls(document.acquisition.pk, document.source_pages)
        return extraction if extraction.run is not None else None

    def reader_page(self, source: int) -> Optional[int]:
        return self._reader_page.get(source)

    def source_page(self, reader_page: int) -> Optional[int]:
        if 1 <= reader_page <= len(self.source_pages):
            return self.source_pages[reader_page - 1]
        return None

    # Regions (Phase 5)

    def page_regions(self, reader_page: int) -> List[dict]:
        source = self.source_page(reader_page)
        if source is None:
            return []
        return [_region_line(region, reader_page) for region in self.run.regions.filter(page=source)]

    def all_regions(self, kind: Optional[str] = None) -> List[dict]:
        """Every region on a page of the copy, in reading order (IP-018 Phase 8)."""
        regions = self.run.regions.filter(page__in=list(self._reader_page))
        if kind:
            regions = regions.filter(kind=kind)
        lines = []
        for region in regions.order_by("page", "seq"):
            line = _region_line(region, self.reader_page(region.page))
            if region.description and region.description != line["preview"]:
                line["description"] = region.description[:300]
            lines.append(line)
        return lines

    def region(self, region_id) -> Optional[dict]:
        region = self.run.regions.filter(pk=region_id).first()
        page = region and self.reader_page(region.page)
        if not page:
            return None
        return {
            "id": str(region.pk),
            "kind": region.kind,
            "page": page,
            "caption": region.caption,
            "printed_caption": region.printed_caption,
            "description": region.description,
            "category": region.category,
            "markdown": region.markdown,
            "latex": region.latex,
        }

    def region_text(self, region_id) -> Optional[tuple]:
        """What an explanation of a region starts from: `(text, reader_page)`."""
        region = self.region(region_id)
        if region is None:
            return None
        label = {"figure": "Figure", "table": "Table", "equation": "Equation"}[region["kind"]]
        parts = [f"{label} on page {region['page']}."]
        if region["printed_caption"]:
            parts.append(f"Printed caption: {region['printed_caption']}")
        if region["description"]:
            parts.append(f"Description: {region['description']}")
        if region["caption"] and not (region["printed_caption"] or region["description"]):
            parts.append(f"Caption: {region['caption']}")
        if region["markdown"]:
            parts.append(f"Table content:\n{region['markdown']}")
        if region["latex"]:
            parts.append(f"LaTeX: {region['latex']}")
        return "\n".join(parts), region["page"]

    # Paragraphs (Phase 6b)

    @property
    def embedding_model(self) -> Optional[str]:
        provenance = (self.run.provenance or {}).get("paragraphs") or {}
        return provenance.get("embedding_model")

    def has_paragraphs(self) -> bool:
        return self.run.paragraphs.filter(embedding__isnull=False).exists()

    def embed(self, text: str, task: str = "passages") -> List[float]:
        """The request, embedded by `tei-embed` (IP-021) — the model that embeds the paragraphs.

        Replaces the Celery round-trip to text-service's embed worker (30 s wait).
        """
        from apps.core.search import embeddings

        try:
            return embeddings.embed_query(text, task)
        except embeddings.EmbeddingError as error:
            raise EmbeddingUnavailable(str(error)) from error

    def search_paragraphs(self, vector: List[float], min_score: float, limit: int) -> List[Candidate]:
        """Paragraphs above `min_score`, best first, expanded one hop over `next` and `similar` links."""
        sources = [source for source in self.source_pages if source]
        ranked = (
            self.run.paragraphs.filter(embedding__isnull=False, page__in=sources)
            .annotate(distance=CosineDistance("embedding", vector))
            .filter(distance__lte=1 - min_score)
            .order_by("distance")[:limit]
        )
        candidates: Dict = {}
        for paragraph in ranked:
            candidates[paragraph.pk] = Candidate(paragraph, round(1 - paragraph.distance, 4), 0)

        # A passage that continues in the next paragraph, or makes the same point elsewhere.
        if candidates:
            links = ParagraphLink.objects.filter(source_id__in=list(candidates)).select_related("target")
            for link in links:
                target = link.target
                if target.pk in candidates or target.page not in self._reader_page:
                    continue
                if link.kind == ParagraphLink.Kind.SIMILAR and (link.score or 0) < min_score:
                    continue
                score = candidates[link.source_id].score
                candidates[target.pk] = Candidate(target, round(link.score if link.score else score, 4), 0)

        result = list(candidates.values())[: limit * 2]
        sentences: Dict = {}
        for sentence in Sentence.objects.filter(paragraph_id__in=[c.paragraph.pk for c in result]).order_by("seq"):
            sentences.setdefault(sentence.paragraph_id, []).append(sentence)
        for candidate in result:
            candidate.page = self.reader_page(candidate.paragraph.page)
            candidate.sentences = sentences.get(candidate.paragraph.pk, [])
        # Reading order inside the model's view; the score travels with each paragraph.
        return sorted((c for c in result if c.page), key=lambda c: c.paragraph.seq)

    def page_size(self, source: int) -> tuple:
        sizes = self.run.page_sizes or []
        if 1 <= source <= len(sizes):
            return tuple(sizes[source - 1])
        return (595.0, 842.0)


def _region_line(region: Region, reader_page: int) -> dict:
    preview = region.caption or (region.markdown or "").split("\n", 1)[0] or region.latex or ""
    return {"id": str(region.pk), "kind": region.kind, "page": reader_page, "preview": preview[:300]}
