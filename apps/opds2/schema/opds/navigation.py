from typing import Optional

from pydantic import BaseModel


class NavigationEntry(BaseModel):
    """OPDS 2.0 Navigation Entry.

    https://drafts.opds.io/opds-2.0#3-navigation
    """

    href: str
    title: str
    type: Optional[str] = None
    rel: Optional[list[str] | str] = None
