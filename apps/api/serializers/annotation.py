from datetime import datetime
from uuid import UUID

from apps.api.serializers import Serializer


class AnnotationSerializer:
    class Base(Serializer):
        id: UUID
        user_acquisition_id: UUID
        title: str
        #: Viewer layer: `drawing`, `highlight`, `agent` or `bookmarks` (IP-018).
        kind: str
        status: str
        created_at: datetime
        updated_at: datetime


class AnnotationItemSerializer:
    class Base(Serializer):
        id: UUID
        annotation_id: UUID
        page: int
        content: str
        created_at: datetime
        updated_at: datetime
