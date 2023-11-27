from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import computed_field

from apps.api.serializers import Serializer
from apps.core.auth import JWTFactory


class ApiKeySerializer:
    class Base(Serializer):
        id: UUID
        user_id: UUID
        name: Optional[str]
        is_active: bool
        last_seen_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime

        @computed_field
        def token(self) -> str:
            return JWTFactory(str(self.user_id)).api_key(str(self.id))
