from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from apps.opds2.schema.rwpm import Link


class LcpUser(BaseModel):
    """LCP License User info."""

    id: str
    email: Optional[str] = None
    name: Optional[str] = None
    encrypted: Optional[list[str]] = None


class LcpUserKey(BaseModel):
    """LCP License User Key."""

    model_config = ConfigDict(populate_by_name=True)

    text_hint: Optional[str] = Field(None, alias="text_hint")
    hex_value: Optional[str] = Field(None, alias="hex_value")
    algorithm: Optional[str] = None


class LcpContentKey(BaseModel):
    """LCP License Content Key."""

    model_config = ConfigDict(populate_by_name=True)

    algorithm: Optional[str] = None
    encrypted_value: Optional[str] = Field(None, alias="encrypted_value")


class LcpEncryption(BaseModel):
    """LCP License Encryption."""

    model_config = ConfigDict(populate_by_name=True)

    profile: Optional[str] = None
    content_key: Optional[LcpContentKey] = Field(None, alias="content_key")
    user_key: Optional[LcpUserKey] = Field(None, alias="user_key")


class LcpRights(BaseModel):
    """LCP License Rights."""

    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    print: Optional[int] = None
    copy: Optional[int] = None


class LcpLicense(BaseModel):
    """Typed LCP License Document.

    https://readium.org/lcp-specs/releases/lcp/latest
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    issued: Optional[datetime] = None
    updated: Optional[datetime] = None
    provider: Optional[str] = None
    encryption: Optional[LcpEncryption] = None
    links: Optional[list[Link]] = None
    user: Optional[LcpUser] = None
    rights: Optional[LcpRights] = None
    signature: Optional[dict] = None
