from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from django.db.models import Prefetch
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
        title: Dict[str, str]
        kind: Feed.FeedKind
        url_name: str
        url: str = Field(default=None, validate_default=True)
        content: str
        per_page: Optional[int] = None
        touched_at: datetime
        created_at: datetime
        updated_at: datetime

        # Iterate `.all()` so a `parents`/`children` prefetch is reused; `values_list` on a
        # related manager is always a new query, three per feed in an entry list (#124).
        @field_validator("parents", mode="before")
        def generate_parents(cls, v, info: ValidationInfo) -> List[UUID]:
            return [feed.pk for feed in v.all()]

        @field_validator("children", mode="before")
        def generate_children(cls, v, info: ValidationInfo) -> List[UUID]:
            return [feed.pk for feed in v.all()]

        @field_validator("url", mode="before")
        def generate_absolute_url(cls, v, info: ValidationInfo) -> Optional[UUID]:
            return info.context["request"].build_absolute_uri(v)

        class Meta:
            examples = {}


def feed_queryset():
    """
    Feeds with what `FeedSerializer.Base` reads: `catalog` (for `url`), `parents` and `children`.
    Of a parent or child the serializer reads only the id: no other column is loaded for them.
    """
    return Feed.objects.select_related("catalog").prefetch_related(
        Prefetch("parents", queryset=Feed.objects.only("id")),
        Prefetch("children", queryset=Feed.objects.only("id")),
    )
