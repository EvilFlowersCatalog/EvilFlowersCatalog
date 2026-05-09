from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import Field, computed_field

from apps.api.serializers import Serializer
from apps.api.serializers.entries import EntrySerializer
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

        @computed_field
        @property
        def download_url(self) -> str:
            """URL to download the .lcpl license file for reading apps."""
            return f"/readium/v1/licenses/{self.id}.lcpl"

    class Detailed(Base):
        entry: EntrySerializer.Base
