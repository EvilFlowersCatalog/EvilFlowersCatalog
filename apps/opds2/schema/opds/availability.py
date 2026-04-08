from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class Availability(BaseModel):
    """OPDS 2.0 Availability.

    https://drafts.opds.io/opds-2.0#4-acquisition-objects
    """

    state: Literal["available", "unavailable", "reserved"]
    since: Optional[datetime] = None
    until: Optional[datetime] = None


class Copies(BaseModel):
    """OPDS 2.0 Copies information."""

    total: Optional[int] = None
    available: Optional[int] = None
