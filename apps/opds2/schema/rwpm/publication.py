from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .link import Link
from .metadata import Metadata


class Publication(BaseModel):
    """Readium Web Publication Manifest.

    https://readium.org/webpub-manifest/
    Minimal viable set for PDF/EPUB publications.
    """

    model_config = ConfigDict(populate_by_name=True)

    context: Optional[list[str]] = Field(None, alias="@context")
    metadata: Metadata
    links: Optional[list[Link]] = None
    reading_order: Optional[list[Link]] = Field(None, alias="readingOrder")
    resources: Optional[list[Link]] = None
    images: Optional[list[Link]] = None
