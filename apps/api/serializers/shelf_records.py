from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class ShelfRecordSerializer:
    class Base(Serializer):
        user_id: UUID
        entry_id: UUID
        created_at: datetime
        updated_at: datetime
