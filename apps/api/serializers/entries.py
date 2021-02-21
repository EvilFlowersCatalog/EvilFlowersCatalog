from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class EntrySerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        catalog_id: UUID
        author_id: UUID = None
        category_id: UUID = None
        language_id: UUID = None
        title: str
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        summary: str = None
        content: str = None
