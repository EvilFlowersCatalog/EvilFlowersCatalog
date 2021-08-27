from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class CatalogSerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        url_name: str
        title: str
        is_public: bool
        created_at: datetime
        updated_at: datetime
