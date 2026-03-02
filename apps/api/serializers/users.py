from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import Field, computed_field

from apps.api.serializers import Serializer
from apps.core.models import UserCatalog


class UserSerializer:
    class Minimal(Serializer):
        id: UUID
        username: str
        name: str
        surname: str

    class Base(Minimal):
        is_superuser: bool
        is_active: bool
        last_login: Optional[datetime]
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        permissions: List[str]
        catalog_permissions: dict[UUID, UserCatalog.Mode] = Field(default=dict)
        lcp_passphrase_hash: Optional[str] = Field(default=None, exclude=True)
        lcp_passphrase_hint: Optional[str] = None

        @computed_field
        @property
        def has_lcp_passphrase(self) -> bool:
            return bool(self.lcp_passphrase_hash)
