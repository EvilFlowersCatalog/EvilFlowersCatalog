from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, AnyUrl


class LicenseStatus(Enum):
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


class EventType(Enum):
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
    links: Optional[List] = None
    id: str = Field(..., description="Unique identifier for the License Document associated to the Status Document.")
    status: LicenseStatus = Field(..., description="Current status of the License.")
    message: str = Field(
        ..., description="A message meant to be displayed to the User regarding the current status of the license."
    )
    updated: Updated
    potential_rights: Optional[PotentialRights] = None
    events: Optional[List[Event]] = None


class Schema(BaseModel):
    href: str = Field(..., description="URI or URI template of the linked resource")
    type: Optional[str] = Field(None, description="MIME type of the linked resource")
    templated: Optional[bool] = Field(None, description="Indicates that a URI template is used in href")
    title: Optional[str] = Field(None, description="Title of the linked resource")
    rel: List[str] = Field(..., description="Relation between the linked resource and its containing collection")
    profile: Optional[AnyUrl] = Field(None, description="Expected profile used to identify the external resource")
    length: Optional[int] = Field(None, description="Content length in octets")
    hash: Optional[str] = Field(None, description="SHA-256 hash of the resource")
