from datetime import datetime
from typing import Optional
from uuid import UUID

from django.core.cache import cache
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
            cache_key = f"api_key.{self.id}.token"
            cached = cache.get(cache_key)

            if cached:
                return cached

            result = JWTFactory(str(self.user_id)).api_key(str(self.id))
            cache.set(cache_key, result)

            return result
