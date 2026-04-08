from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from apps.opds2.schema.rwpm import Link


class LicenseStatus(str, Enum):
    ready = "ready"
    active = "active"
    revoked = "revoked"
    returned = "returned"
    cancelled = "cancelled"
    expired = "expired"


class Updated(BaseModel):
    license: datetime = Field(..., description="Time and Date when the License Document was last updated")
    status: datetime = Field(..., description="Time and Date when the Status Document was last updated")


class PotentialRights(BaseModel):
    end: Optional[datetime] = Field(None, description="Date and time when the license ends")


class EventType(str, Enum):
    register = "register"
    renew = "renew"
    return_ = "return"
    revoke = "revoke"
    cancel = "cancel"


class Event(BaseModel):
    type: Optional[EventType] = Field(None, description="Identifies the type of event")
    name: Optional[str] = Field(
        None, description="Name of the client, as provided by the client during an interaction"
    )
    id: Optional[str] = Field(
        None, description="Identifies the client, as provided by the client during an interaction"
    )
    timestamp: Optional[datetime] = Field(None, description="Time and date when the event occurred")


class ReadiumLcpStatusDocument(BaseModel):
    """LCP License Status Document.

    https://readium.org/lcp-specs/releases/lsd/latest
    """

    id: str = Field(..., description="Unique identifier for the License Document associated to the Status Document.")
    status: LicenseStatus = Field(..., description="Current status of the License.")
    message: str = Field(
        ..., description="A message meant to be displayed to the User regarding the current status of the license."
    )
    updated: Updated
    links: Optional[list[Link]] = None
    potential_rights: Optional[PotentialRights] = None
    events: Optional[list[Event]] = None
