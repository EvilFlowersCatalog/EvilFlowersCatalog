from datetime import datetime
from typing import List, Optional
from uuid import UUID

from apps.api.serializers import Serializer


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
