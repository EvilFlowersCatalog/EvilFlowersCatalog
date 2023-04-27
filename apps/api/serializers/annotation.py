from datetime import datetime
from uuid import UUID

from porcupine.base import Serializer


class AnnotationSerializer:
    class Base(Serializer):
        id: UUID
        user_acquisition_id: UUID
        content: str
        created_at: datetime
        updated_at: datetime