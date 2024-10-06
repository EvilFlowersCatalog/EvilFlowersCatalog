from apps.api.serializers import Serializer
from datetime import datetime
from typing import Optional


class StatusStatistics(Serializer):
    catalogs: int
    entries: int
    acquisitions: int
    users: int


class StatusSerializer(Serializer):
    timestamp: datetime
    instance: str
    stats: StatusStatistics
    build: Optional[str] = None
    version: Optional[str] = None
    python: Optional[str] = None
    supervisord: Optional[dict[str, str]] = None
