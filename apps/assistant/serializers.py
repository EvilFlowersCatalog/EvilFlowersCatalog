from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import Field, field_validator

from apps.api.serializers import Serializer


class ChatMessageSerializer:
    class Base(Serializer):
        id: UUID
        role: str
        text: str
        displayed_entries: Optional[list]
        tokens_used: int
        created_at: datetime


class ChatSerializer:
    class Base(Serializer):
        id: UUID
        user_id: UUID
        entry_id: Optional[UUID]
        catalog_id: Optional[UUID]
        title: str
        is_active: bool
        message_count: int
        total_tokens: int
        last_message_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        # noinspection PyDataclass
        messages: List[ChatMessageSerializer.Base] = Field(default_factory=list, validate_default=True)

        @field_validator("messages", mode="before")
        def _resolve_messages(cls, v):
            # `chat.messages` is a related manager, which pydantic cannot
            # iterate; the model's Meta orders these by `created_at`. Tool
            # traffic is internal to the replayed conversation and not shown.
            return v.visible() if hasattr(v, "visible") else v
