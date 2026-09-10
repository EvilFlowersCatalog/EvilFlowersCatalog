from apps.api.serializers import Serializer
from datetime import datetime
from typing import Optional, List


class StatusStatistics(Serializer):
    catalogs: int
    entries: int
    acquisitions: int
    users: int


class StatusClientIP(Serializer):
    remote_addr: Optional[str] = None
    x_real_ip: Optional[str] = None
    x_forwarded_for: Optional[str] = None
    resolved: str


class StatusSerializer(Serializer):
    timestamp: datetime
    instance: str
    stats: StatusStatistics
    build: Optional[str] = None
    version: Optional[str] = None
    python: Optional[str] = None
    supervisord: Optional[dict[str, str]] = None
    client_ip: Optional[StatusClientIP] = None
    allowed_ip_ranges: Optional[List[str]] = None
