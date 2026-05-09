from typing import Optional

from pydantic import BaseModel, ConfigDict

from apps.opds2.schema.rwpm import Link, Metadata, Publication

from .navigation import NavigationEntry


class Group(BaseModel):
    """OPDS 2.0 Group.

    https://drafts.opds.io/opds-2.0#6-groups
    """

    model_config = ConfigDict(populate_by_name=True)

    metadata: Optional[Metadata] = None
    links: Optional[list[Link]] = None
    publications: Optional[list[Publication]] = None
    navigation: Optional[list[NavigationEntry]] = None
