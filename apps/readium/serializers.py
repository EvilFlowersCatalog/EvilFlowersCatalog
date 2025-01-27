from datetime import datetime
from typing import Optional
from uuid import UUID

from apps.api.serializers import Serializer
from apps.readium.models import License


class LicenseSerializer:
    class Base(Serializer):
        id: UUID
        entry_id: UUID
        user_id: UUID
        state: License.LicenseState
        starts_at: Optional[datetime]
        expires_at: Optional[datetime]
        created_at: datetime
        updated_at: datetime
