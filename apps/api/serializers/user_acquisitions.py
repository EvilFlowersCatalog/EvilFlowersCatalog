from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer

from apps.api.serializers.entries import AcquisitionSerializer, EntrySerializer
from apps.api.serializers.users import UserSerializer
from apps.core.models import UserAcquisition


class UserAcquisitionSerializer:
    class Base(Serializer):
        id: UUID
        type: UserAcquisition.UserAcquisitionType
        range: str = None
        user: UserSerializer.Minimal
        acquisition: AcquisitionSerializer.Nested
        url: str
        entry: dict
        expire_at: datetime = None
        created_at: datetime
        updated_at: datetime

        @staticmethod
        def resolve_entry(data: UserAcquisition) -> dict:
            return EntrySerializer.Base(data.acquisition.entry).dict()
