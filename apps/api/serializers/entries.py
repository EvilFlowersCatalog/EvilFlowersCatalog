from datetime import datetime
from typing import List
from uuid import UUID

from porcupine.base import Serializer

from apps.core.models import Acquisition


class AuthorSerializer:
    class Base(Serializer):
        id: UUID
        name: str
        surname: str


class CategorySerializer:
    class Base(Serializer):
        id: UUID
        term: str

    class Detail(Base):
        label: str = None
        scheme: str = None


class LanguageSerializer:
    class Base(Serializer):
        id: UUID
        name: str
        code: str


class AcquisitionSerializer:
    class Nested(Serializer):
        relation: Acquisition.AcquisitionType
        mime: Acquisition.AcquisitionMIME
        url: str = None

    class Base(Nested):
        id: UUID


class EntrySerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        catalog_id: UUID
        author: AuthorSerializer.Base = None
        category: CategorySerializer.Base = None
        language: LanguageSerializer.Base = None
        title: str
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        summary: str = None
        content: str = None
        identifiers: List[str] = None
        acquisitions: List[AcquisitionSerializer.Nested] = None
        # contributors: List[AuthorSerializer.Base] = None
