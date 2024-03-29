from datetime import datetime
from typing import Optional
from uuid import UUID

from apps.api.serializers import Serializer
from apps.api.serializers.entries import AcquisitionSerializer, EntrySerializer
from apps.api.serializers.users import UserSerializer
from apps.core.models import UserAcquisition


class UserAcquisitionSerializer:
    class Base(Serializer):
        id: UUID
        type: UserAcquisition.UserAcquisitionType
        range: Optional[str]
        user: UserSerializer.Minimal
        acquisition: AcquisitionSerializer.Nested
        url: str
        entry: EntrySerializer.Base
        expire_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime
