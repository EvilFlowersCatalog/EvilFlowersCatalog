from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import field_validator, Field
from pydantic_core.core_schema import ValidationInfo

from apps.api.serializers import Serializer
from apps.core.models import Feed


class FeedSerializer:
    class Base(Serializer):
        id: UUID
        catalog_id: UUID
        parents: List[UUID] = Field(default=[], validate_default=True)
        children: List[UUID] = Field(default=[], validate_default=True)
        creator_id: UUID
        title: str
        kind: Feed.FeedKind
        url_name: str
        url: str
        content: str
        per_page: Optional[int]
        touched_at: datetime
        created_at: datetime
        updated_at: datetime

        @field_validator("parents", mode="before")
        def generate_parents(cls, v, info: ValidationInfo) -> List[UUID]:
            return v.all().values_list("id", flat=True)

        @field_validator("children", mode="before")
        def generate_children(cls, v, info: ValidationInfo) -> List[UUID]:
            return v.all().values_list("id", flat=True)
