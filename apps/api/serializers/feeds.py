from datetime import datetime
from typing import List
from uuid import UUID

from porcupine.base import Serializer

from apps.core.models import Feed


class FeedSerializer:
    class Base(Serializer):
        id: UUID
        catalog_id: UUID
        parents: list = []
        children: list = []
        creator_id: UUID
        title: str
        kind: Feed.FeedKind
        url_name: str
        url: str
        content: str
        per_page: int = None
        created_at: datetime
        updated_at: datetime

        @staticmethod
        def resolve_parents(data: Feed) -> List[UUID]:
            return list(data.parents.all().values_list('id', flat=True))

        @staticmethod
        def resolve_children(data: Feed) -> List[UUID]:
            return list(data.children.all().values_list('id', flat=True))
