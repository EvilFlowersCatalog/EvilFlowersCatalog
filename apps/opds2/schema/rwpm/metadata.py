from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .belongs_to import BelongsTo
from .contributor import Contributor
from .subject import Subject


class Metadata(BaseModel):
    """RWPM Metadata.

    https://readium.org/webpub-manifest/schema/metadata.schema.json
    Minimal viable set for PDF/EPUB publications.
    """

    model_config = ConfigDict(populate_by_name=True)

    identifier: Optional[str] = None
    type: Optional[str] = Field(None, alias="@type")
    title: str
    subtitle: Optional[str] = None
    modified: Optional[datetime] = None
    published: Optional[str] = None
    language: Optional[list[str]] = None
    sort_as: Optional[str] = Field(None, alias="sortAs")
    author: Optional[list[Contributor]] = None
    translator: Optional[list[Contributor]] = None
    editor: Optional[list[Contributor]] = None
    publisher: Optional[list[Contributor]] = None
    subject: Optional[list[Subject]] = None
    description: Optional[str] = None
    belongs_to: Optional[BelongsTo] = Field(None, alias="belongsTo")
    number_of_pages: Optional[int] = Field(None, alias="numberOfPages")
