from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class UserSerializer:
    class Base(Serializer):
        id: UUID
        email: str
        name: str
        surname: str
        is_superuser: bool
        is_active: bool
        last_login: datetime = None
        created_at: datetime
        updated_at: datetime
