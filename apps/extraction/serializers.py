from datetime import datetime
from typing import Dict, Optional
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


class RegionSerializer(Serializer):
    id: UUID
    kind: str
    #: The reader's page number, as the viewer shows it.
    page: int
    seq: int
    #: `{x0, y0, x1, y1}` page fractions, origin top-left.
    bbox: Dict[str, float]
    caption: Optional[str]
    category: Optional[str]
    markdown: Optional[str]
    latex: Optional[str]
    #: Authorized route of the crop, or None when the service stored none.
    image_url: Optional[str]
