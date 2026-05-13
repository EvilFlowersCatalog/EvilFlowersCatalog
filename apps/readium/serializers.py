from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import Field, computed_field

from apps.api.serializers import Serializer
from apps.api.serializers.entries import EntrySerializer
from apps.readium.models import License, Reservation


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

        @computed_field
        @property
        def reader_requirements(self) -> dict:
            return {
                "reader": "Thorium Reader",
                "reader_url": "https://www.edrlab.org/software/thorium-reader/",
                "note": "LCP-protected publications can only be opened in an LCP-compliant reader.",
            }

    class Detailed(Base):
        entry: EntrySerializer.Base


class ReservationSerializer:
    class Base(Serializer):
        id: UUID
        entry_id: UUID
        user_id: UUID
        position: int
        status: Reservation.Status
        requested_at: datetime
        available_at: Optional[datetime] = None
        claim_deadline: Optional[datetime] = None
        claimed_license_id: Optional[UUID] = None
        created_at: datetime
        updated_at: datetime
