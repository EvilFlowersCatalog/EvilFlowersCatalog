from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import field_validator, Field
from pydantic_core.core_schema import ValidationInfo

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
        url: str = Field(default=None, validate_default=True)
        entry: EntrySerializer.Base
        expire_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime

        @field_validator("url", mode="before")
        def generate_absolute_url(cls, v, info: ValidationInfo) -> Optional[UUID]:
            return info.context["request"].build_absolute_uri(v)
