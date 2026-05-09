from typing import Optional

from pydantic import BaseModel, ConfigDict

from apps.opds2.schema.rwpm import Link, Metadata, Publication

from .facet import Facet
from .group import Group
from .navigation import NavigationEntry


class OpdsFeed(BaseModel):
    """OPDS 2.0 Feed.

    https://drafts.opds.io/opds-2.0
    """

    model_config = ConfigDict(populate_by_name=True)

    metadata: Metadata
    links: Optional[list[Link]] = None
    navigation: Optional[list[NavigationEntry]] = None
    publications: Optional[list[Publication]] = None
    groups: Optional[list[Group]] = None
    facets: Optional[list[Facet]] = None
