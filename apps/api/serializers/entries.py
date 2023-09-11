from datetime import datetime
from typing import List, Optional, Dict
from uuid import UUID

from porcupine.base import Serializer

from apps.api.serializers.feeds import FeedSerializer
from apps.core.models import Acquisition, Entry, ShelfRecord


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
        label: str = None
        scheme: str = None


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
        url: str = None

    class Base(Nested):
        id: UUID

    class Detailed(Base):
        content: str = None
        checksum: str = None

        @staticmethod
        def resolve_content(data: Acquisition, **kwargs) -> Optional[str]:
            return data.base64


class EntrySerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        catalog_id: UUID
        shelf_record_id: Optional[UUID]
        author: AuthorSerializer.Base = None
        categories: List[CategorySerializer.Base] = None
        language: LanguageSerializer.Base = None
        feeds: List[FeedSerializer.Base] = None
        popularity: int
        title: str
        image: str = None
        image_mime: str = None
        thumbnail: str = None
        config: dict = None
        citation: str = None
        created_at: datetime
        updated_at: datetime

        @staticmethod
        def resolve_shelf_record_id(data: Entry, **kwargs) -> Optional[str]:
            if 'request' in kwargs:
                try:
                    return data.shelf_records.get(user=kwargs['request'].user).id
                except ShelfRecord.DoesNotExist:
                    return None

            return None

        @staticmethod
        def resolve_image(data: Entry, **kwargs) -> Optional[str]:
            if not data.image:
                return None
            return data.image_url

        @staticmethod
        def resolve_thumbnail(data: Entry, **kwargs) -> Optional[str]:
            if not data.image:
                return None
            return data.thumbnail_base64

    class Detailed(Base):
        summary: str = None
        content: str = None
        identifiers: Dict = None
        acquisitions: List[AcquisitionSerializer.Base] = None
        contributors: List[AuthorSerializer.Base] = None
