from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from apps.api.serializers import Serializer


class ExtractionRunSerializer:
    class Base(Serializer):
        id: UUID
        acquisition_id: UUID
        pipeline_version: int
        status: str
        is_active: bool
        page_count: Optional[int]
        #: `{job: pending | done | failed}`.
        jobs: Dict[str, str]
        #: The current attempt (#140): send it back as `?attempt=` with every result of this pass.
        attempt: int
        error: Optional[str]
        #: Where the services write this run's crops, relative to the storage root.
        storage_dir: str
        started_at: Optional[datetime]
        finished_at: Optional[datetime]
        created_at: datetime


class ExtractionStatusSerializer(Serializer):
    """What a reader's client needs to tell "nothing found" from "not processed yet"."""

    #: `none` when the publication was never queued, else the latest run's status.
    status: str
    pipeline_version: Optional[int]
    #: True when an active run is readable, even while a newer one is in progress.
    available: bool
    figures: int = 0
    tables: int = 0
    equations: int = 0
    paragraphs: int = 0


class ReadiumLocatorSerializer(Serializer):
    """The locator as a Readium Locator (readium/architecture `models/locators`), for a Readium-based viewer."""

    #: The publication resource: the acquisition's download route.
    href: Optional[str]
    type: str
    #: `{position: reader page, fragments: ["page=N"]}`.
    locations: Dict[str, object]
    #: `{before, highlight, after}`; empty where the reader may not read the text.
    text: Dict[str, str]


class LocatorSerializer(Serializer):
    """Where a run-independent address points now (#171)."""

    key: str
    entry_id: UUID
    #: `paragraph | section | figure | table | equation`.
    kind: str
    #: `exact` (the active run has the key), `relocated` (found by its quote), `page_only` (the page only).
    resolution: str
    #: The reader's page (the license page counted), as the viewer shows it.
    page: int
    #: The page of the original file.
    source_page: int
    #: `{x0, y0, x1, y1}` page fractions of the paragraph or region; None for `page_only` and sections.
    bbox: Optional[Dict[str, float]] = None
    #: Quads of the paragraph's best sentence (the one sharing most words with `?sentence=`, else the quote).
    quads: List[Dict[str, float]] = []
    #: Whether the text may be shown outside the viewer (never Readium LCP, never outside the IP ranges).
    readable: bool
    #: The paragraph's text, the caption or the heading; only where `readable`.
    quote: Optional[str] = None
    #: When a newer run stopped having the key; null while the active run has it.
    detached_at: Optional[datetime] = None
    readium: ReadiumLocatorSerializer


class RegionSerializer(Serializer):
    id: UUID
    kind: str
    #: The reader's page number, as the viewer shows it.
    page: int
    seq: int
    #: `{x0, y0, x1, y1}` page fractions, origin top-left.
    bbox: Dict[str, float]
    #: The printed caption, else the model's description.
    caption: Optional[str]
    #: The caption printed in the document, verbatim.
    printed_caption: Optional[str] = None
    #: A multimodal model's description of a figure.
    description: Optional[str] = None
    category: Optional[str]
    markdown: Optional[str]
    latex: Optional[str]
    #: Authorized route of the crop, or None when the service stored none.
    image_url: Optional[str]
