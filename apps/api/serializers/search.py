from typing import Any, Optional

from pydantic import Field

from apps.api.serializers import Serializer
from apps.api.serializers.entries import EntrySerializer


class EntrySearchHitSerializer(Serializer):
    entry: EntrySerializer.Base
    score: Optional[float] = None
    chunk_id: Optional[str] = None
    highlight: Optional[dict[str, Any]] = None
    source: Optional[dict[str, Any]] = None


class EntrySearchResponseSerializer(Serializer):
    search_type: str
    query: str
    total_results: int
    results: list[EntrySearchHitSerializer] = Field(default_factory=list)

