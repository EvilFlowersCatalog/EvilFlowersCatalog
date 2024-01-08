from datetime import datetime
from uuid import UUID

from apps.api.serializers import Serializer
from apps.api.serializers.entries import EntrySerializer


class ShelfRecordSerializer:
    class Base(Serializer):
        id: UUID
        user_id: UUID
        entry: EntrySerializer.Base
        created_at: datetime
        updated_at: datetime
