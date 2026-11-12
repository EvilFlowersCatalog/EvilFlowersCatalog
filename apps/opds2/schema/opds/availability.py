from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class Availability(BaseModel):
    """OPDS 2.0 Availability.

    https://drafts.opds.io/schema/properties.schema.json
    """

    state: Literal["available", "unavailable", "reserved", "ready"]
    since: Optional[datetime] = None
    until: Optional[datetime] = None


class Copies(BaseModel):
    """OPDS 2.0 Copies information."""

    total: Optional[int] = None
    available: Optional[int] = None


class Holds(BaseModel):
    """OPDS 2.0 Holds: the queue length and the user's place in it."""

    total: Optional[int] = None
    position: Optional[int] = None
