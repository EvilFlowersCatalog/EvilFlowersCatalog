from datetime import datetime
from uuid import UUID

from apps.api.serializers import Serializer


class NotificationContactSerializer:
    class Base(Serializer):
        id: UUID
        type: str
        value: str
        is_primary: bool
        created_at: datetime
        updated_at: datetime
