from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import field_validator, Field, model_validator
from pydantic_core.core_schema import ValidationInfo

from apps.api.serializers import Serializer
from apps.api.serializers.entries import AcquisitionSerializer, EntrySerializer
from apps.api.serializers.users import UserSerializer
from apps.core.models import UserAcquisition
from apps.files import signing


class UserAcquisitionSerializer:
    class Base(Serializer):
        id: UUID
        type: UserAcquisition.UserAcquisitionType
        range: Optional[str]
        user: UserSerializer.Minimal
        acquisition: AcquisitionSerializer.Nested
        url: str = Field(default=None, validate_default=True)
        # #214: `url` with a signed, reusable credential in the query, for pdf.js range streaming: no
        # `Authorization` header (no CORS preflight, no token expiring mid-book). For the owner only.
        stream_url: Optional[str] = Field(
            default=None,
            description="The download URL with a signed, reusable credential (the owner only); "
            "valid until `stream_url_expires_at`.",
        )
        stream_url_expires_at: Optional[datetime] = None
        entry: EntrySerializer.Base
        expire_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime

        @field_validator("url", mode="before")
        def generate_absolute_url(cls, v, info: ValidationInfo) -> Optional[UUID]:
            return info.context["request"].build_absolute_uri(v)

        @model_validator(mode="wrap")
        @classmethod
        def sign_stream_url(cls, value, handler, info: ValidationInfo):
            result = handler(value)
            context = info.context or {}
            user = context.get("user")
            if not isinstance(value, UserAcquisition) or user is None or not user.is_authenticated:
                return result
            if value.user_id != user.pk:
                return result
            query, expires_at = signing.signed_query(value)
            result.stream_url = f"{result.url}?{query}"
            result.stream_url_expires_at = expires_at
            return result
