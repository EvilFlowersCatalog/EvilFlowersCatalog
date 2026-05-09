from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from apps.opds2.schema.rwpm import Link, Metadata


class Facet(BaseModel):
    """OPDS 2.0 Facet.

    https://drafts.opds.io/opds-2.0#5-facets
    """

    model_config = ConfigDict(populate_by_name=True)

    metadata: Metadata
    links: list[Link]
    number_of_items: Optional[int] = Field(None, alias="numberOfItems")
