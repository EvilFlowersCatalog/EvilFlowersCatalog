from datetime import datetime
from typing import Optional
from uuid import UUID

from porcupine.base import Serializer


class ApiKeySerializer:
    class Base(Serializer):
        id: UUID
        user_id: UUID
        name: Optional[str] = None
        is_active: bool
        last_seen_at: datetime = None
        created_at: datetime
        updated_at: datetime
