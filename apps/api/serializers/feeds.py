from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class FeedSerializer:
    class Base(Serializer):
        id: UUID
        catalog_id: UUID
        creator_id: UUID
        title: str
        url_name: str
        url: str
        content: str
        created_at: datetime
        updated_at: datetime
