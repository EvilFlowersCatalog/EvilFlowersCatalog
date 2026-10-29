from datetime import datetime
from typing import List, Optional
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
        #: Text highlights only (kinds `highlight` and `agent`): the marked text and
        #: one `{x0, y0, x1, y1}` page-fraction box per line.
        text: Optional[str] = None
        quads: Optional[List[dict]] = None
        created_at: datetime
        updated_at: datetime
