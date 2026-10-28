from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from apps.api.serializers import Serializer


class ChatMessageSerializer:
    class Base(Serializer):
        id: UUID
        role: str
        text: str
        #: Publication the reader attached to this message, if any.
        entry_id: Optional[UUID]
        #: What the reader attached in the viewer (`apps.assistant.contexts`).
        contexts: Optional[list]
        #: Viewer page the reader was on when they sent the message.
        page: Optional[int]
        #: Pages of the publication the answer was drawn from.
        pages_read: Optional[List[int]]
        displayed_entries: Optional[list]
        tokens_used: int
        created_at: datetime


class ChatEntrySerializer(Serializer):
    id: UUID
    catalog_id: UUID
    title: str
    #: Lent through Readium LCP: not readable in the viewer, so clients offer
    #: borrowing instead of "open in reader".
    readium_enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_entry(cls, value):
        if hasattr(value, "read_config"):
            return {
                "id": value.pk,
                "catalog_id": value.catalog_id,
                "title": value.title,
                "readium_enabled": bool(value.read_config("readium_enabled")),
            }
        return value


class ChatSerializer:
    class Base(Serializer):
        id: UUID
        user_id: UUID
        entry_id: Optional[UUID]
        #: The publication the chat is about, for listing chats without a lookup per row.
        entry: Optional[ChatEntrySerializer]
        #: The reader's copy the chat is bound to, and its acquisition (file),
        #: so clients can reopen the publication in the viewer.
        user_acquisition_id: Optional[UUID]
        acquisition_id: Optional[UUID]
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


class ExplanationExampleSerializer(Serializer):
    label: str
    description: str


class ExplanationSerializer:
    class Base(Serializer):
        simple: str
        examples: List[ExplanationExampleSerializer]
        #: Pages the explanation was drawn from, in the order they were read.
        pages: List[int]
