from datetime import datetime
from typing import List
from uuid import UUID

from porcupine.base import Serializer

from apps.api.serializers.users import UserSerializer


class UserCatalog(Serializer):
    mode: str
    user: UserSerializer.Minimal


class CatalogSerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        url_name: str
        title: str
        is_public: bool
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        user_catalogs: List[UserCatalog]
