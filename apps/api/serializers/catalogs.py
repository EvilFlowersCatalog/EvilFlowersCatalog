from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class CatalogSerializer:
    class Base(Serializer):
        id: UUID
        url_name: str
        title: str
        created_at: datetime
        updated_at: datetime
