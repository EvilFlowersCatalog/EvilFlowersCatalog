from datetime import datetime
from typing import List, Optional, Dict
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from apps.api.serializers import Serializer
from apps.api.serializers.feeds import FeedSerializer
from apps.core.models import Acquisition, ShelfRecord


class AuthorSerializer:
    class Base(Serializer):
        id: UUID
        name: str
        surname: str

    class Detailed(Base):
        catalog_id: UUID
        created_at: datetime
        updated_at: datetime


class CategorySerializer:
    class Base(Serializer):
        id: UUID
        term: str

    class Detailed(Base):
        catalog_id: UUID
        label: Optional[str]
        scheme: Optional[str]


class LanguageSerializer:
    class Base(Serializer):
        id: UUID
        name: str
        alpha2: str
        alpha3: str


class AcquisitionSerializer:
    class Nested(Serializer):
        relation: Acquisition.AcquisitionType
        mime: Acquisition.AcquisitionMIME
        url: Optional[str]

    class Base(Nested):
        id: UUID

    class Detailed(Base):
        base64: Optional[str] = Field(serialization_alias="content")
        checksum: Optional[str]


class EntrySerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        catalog_id: UUID
        shelf_record_id: Optional[UUID] = Field(default=None, validate_default=True)
        entry_authors: List[AuthorSerializer.Base] = Field(
            default=[], validate_default=True, serialization_alias="authors"
        )
        categories: List[CategorySerializer.Base] = Field(default=[], validate_default=True)
        language: Optional[LanguageSerializer.Base]
        feeds: List[FeedSerializer.Base] = Field(default=[], validate_default=True)
        popularity: int
        title: str
        image_url: Optional[str] = Field(serialization_alias="image")
        image_mime: Optional[str]
        thumbnail_base64: Optional[str] = Field(serialization_alias="thumbnail")
        config: Optional[dict]
        citation: Optional[str]
        touched_at: datetime
        created_at: datetime
        updated_at: datetime

        @field_validator("shelf_record_id", mode="before")
        def generate_shelf_record_id(cls, v, info: ValidationInfo) -> Optional[UUID]:
            if info.context["user"].is_authenticated:
                try:
                    return info.context["user"].shelf_records.get(entry_id=info.data.get("id")).pk
                except ShelfRecord.DoesNotExist:
                    return None
            return None

        @field_validator("categories", mode="before")
        def generate_categories(cls, v, info: ValidationInfo):
            return v.all()

        @field_validator("feeds", mode="before")
        def generate_feeds(cls, v, info: ValidationInfo):
            return v.all()

        @field_validator("entry_authors", mode="before")
        def generate_authors(cls, v, info: ValidationInfo):
            return [entry_author.author for entry_author in v.order_by("position")]

    class Detailed(Base):
        published_at: Optional[str]
        publisher: Optional[str]
        summary: Optional[str]
        content: Optional[str]
        identifiers: Optional[Dict]
        acquisitions: List[AcquisitionSerializer.Base] = Field(default=[], validate_default=True)

        @field_validator("published_at", mode="before")
        def generate_published_at(cls, v, info: ValidationInfo):
            # TODO: Use Annotated
            # https://docs.pydantic.dev/latest/concepts/types/#composing-types-via-annotated
            return str(v) if v else None

        @field_validator("acquisitions", mode="before")
        def generate_acquisitions(cls, v, info: ValidationInfo):
            return v.all()
