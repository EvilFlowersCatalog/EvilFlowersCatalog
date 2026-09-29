from datetime import datetime
from uuid import UUID

from apps.api.serializers import Serializer
from apps.api.serializers.entries import EntrySerializer
from apps.core.models import UserActivity


class UserActivitySerializer:
    class Base(Serializer):
        id: UUID
        action: UserActivity.ActivityAction
        count: int
        metadata: dict
        entry: EntrySerializer.Base
        created_at: datetime
        last_occurred_at: datetime
