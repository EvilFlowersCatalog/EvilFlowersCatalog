from typing import Optional

from pydantic import BaseModel, ConfigDict

from apps.opds2.schema.rwpm import Link


class AuthenticationFlow(BaseModel):
    """OPDS Authentication Flow.

    https://drafts.opds.io/authentication-for-opds-1.0
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str
    links: Optional[list[Link]] = None
    labels: Optional[dict[str, str]] = None


class AuthenticationDocument(BaseModel):
    """OPDS Authentication Document 1.0.

    https://drafts.opds.io/authentication-for-opds-1.0
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    description: Optional[str] = None
    links: Optional[list[Link]] = None
    authentication: list[AuthenticationFlow]
