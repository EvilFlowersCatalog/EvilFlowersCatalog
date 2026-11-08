from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import computed_field

from apps.api.serializers import Serializer
from apps.core.auth import JWTFactory


class ApiKeySerializer:
    class Base(Serializer):
        """Key metadata. Carries no token: a bearer credential is shown once, at creation."""

        id: UUID
        user_id: UUID
        name: Optional[str]
        is_active: bool
        last_seen_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime

    class Created(Base):
        """Returned by `POST /api/v1/api_keys` only — the one response that includes the token."""

        @computed_field
        def token(self) -> str:
            return JWTFactory(str(self.user_id)).api_key(str(self.id))
