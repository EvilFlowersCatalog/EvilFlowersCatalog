from datetime import datetime
from typing import Optional
from uuid import UUID

from porcupine.base import Serializer

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey


class ApiKeySerializer:
    class Base(Serializer):
        id: UUID
        user_id: UUID
        name: Optional[str] = None
        is_active: bool
        last_seen_at: datetime = None
        created_at: datetime
        updated_at: datetime
        token: str

        @staticmethod
        def resolve_token(data: ApiKey):
            return JWTFactory(str(data.user_id)).api_key(str(data.pk))
