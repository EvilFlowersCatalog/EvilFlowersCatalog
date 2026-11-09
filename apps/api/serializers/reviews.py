from datetime import datetime
from typing import Optional
from uuid import UUID

from apps.api.serializers import Serializer


class ReviewerSerializer(Serializer):
    id: UUID
    name: str
    surname: str


class EntryReviewSerializer:
    class Base(Serializer):
        id: UUID
        entry_id: UUID
        user: ReviewerSerializer
        rating: int
        title: str
        body: str
        language_code: Optional[str] = None
        state: str
        created_at: datetime
        updated_at: datetime

    class Moderated(Base):
        """What the author and moderators see: the moderation details too."""

        moderated_by_id: Optional[UUID] = None
        moderated_at: Optional[datetime] = None
        moderation_note: str = ""

    class Admin(Moderated):
        """`GET /api/v1/reviews`: moderation details plus the reviewed entry."""

        entry: "ReviewedEntrySerializer"


class ReviewedEntrySerializer(Serializer):
    id: UUID
    title: str
    catalog_id: UUID


EntryReviewSerializer.Admin.model_rebuild()


class EntryRatingSerializer(Serializer):
    average: Optional[float]
    count: int
    #: the caller's own rating, absent for anonymous callers or without a review
    mine: Optional[int] = None
