from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import Field

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
