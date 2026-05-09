from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class Properties(BaseModel):
    """Link properties for OPDS 2.0 acquisition objects."""

    model_config = ConfigDict(populate_by_name=True)

    number_of_items: Optional[int] = Field(None, alias="numberOfItems")
    price: Optional[Any] = None
    indirect_acquisition: Optional[list[Link]] = Field(None, alias="indirectAcquisition")
    availability: Optional[Any] = None
    copies: Optional[Any] = None
    authenticate: Optional[Link] = None


class Link(BaseModel):
    """Readium Web Publication Manifest Link Object.

    https://readium.org/webpub-manifest/schema/link.schema.json
    """

    model_config = ConfigDict(populate_by_name=True)

    href: Optional[str] = None
    type: Optional[str] = None
    rel: Optional[list[str] | str] = None
    templated: Optional[bool] = None
    title: Optional[str] = None
    properties: Optional[Properties] = None
    height: Optional[int] = None
    width: Optional[int] = None
    children: Optional[list[Link]] = None


# Rebuild models after all forward refs are available
Properties.model_rebuild()
Link.model_rebuild()
