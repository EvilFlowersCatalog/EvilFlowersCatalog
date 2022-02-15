from datetime import datetime
from typing import List
from uuid import UUID

from porcupine.base import Serializer

from apps.core.models import User


class UserSerializer:
    class Minimal(Serializer):
        id: UUID
        email: str
        name: str
        surname: str

    class Base(Minimal):
        is_superuser: bool
        is_active: bool
        last_login: datetime = None
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        permissions: List

        @staticmethod
        def resolve_permissions(data: User):
            return data.get_all_permissions()
