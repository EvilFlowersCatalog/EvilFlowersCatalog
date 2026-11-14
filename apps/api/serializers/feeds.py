from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import OuterRef
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

        # The ids annotated by `feed_queryset` when present (#141); else iterate `.all()` so a
        # prefetch is reused: `values_list` on a related manager is always a new query (#124).
        @field_validator("parents", mode="before")
        def generate_parents(cls, v, info: ValidationInfo) -> List[UUID]:
            return _related_ids(v, "_parent_ids")

        @field_validator("children", mode="before")
        def generate_children(cls, v, info: ValidationInfo) -> List[UUID]:
            return _related_ids(v, "_child_ids")

        @field_validator("url", mode="before")
        def generate_absolute_url(cls, v, info: ValidationInfo) -> Optional[UUID]:
            return info.context["request"].build_absolute_uri(v)

        class Meta:
            examples = {}


def _related_ids(manager, annotation: str) -> List[UUID]:
    ids = getattr(getattr(manager, "instance", None), annotation, None)
    if ids is not None:
        return list(ids)
    return [feed.pk for feed in manager.all()]


def feed_queryset():
    """
    Feeds with what `FeedSerializer.Base` reads: `catalog` (for `url`) and the ids of `parents` and
    `children`. The ids come as array subselects of `feed_parents` in the same query: as two prefetches
    they were two more round trips on every entry list and search page (#141).
    """
    through = Feed.parents.through
    return Feed.objects.select_related("catalog").annotate(
        _parent_ids=ArraySubquery(
            through.objects.filter(from_feed_id=OuterRef("pk")).order_by("to_feed_id").values("to_feed_id")
        ),
        _child_ids=ArraySubquery(
            through.objects.filter(to_feed_id=OuterRef("pk")).order_by("from_feed_id").values("from_feed_id")
        ),
    )
