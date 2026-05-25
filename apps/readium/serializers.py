from datetime import datetime
from typing import Optional
from uuid import UUID

from django.conf import settings
from django.urls import reverse
from pydantic import Field, ValidationInfo, computed_field, field_validator

from apps.api.serializers import Serializer
from apps.api.serializers.entries import EntrySerializer
from apps.core.services.capability_tokens import CapabilityTokenService
from apps.readium.capability_scopes import (
    LCPL_DOWNLOAD,
    LCPL_FEED_DOWNLOAD,
    lcpl_download_ttl,
    lcpl_feed_download_ttl,
)
from apps.readium.models import License, Reservation


def _mint_lcpl_download_url(license_id: UUID, user_id: UUID, info: ValidationInfo) -> str:
    """
    IP-009 Phase 4 D2: mint a capability token and return the absolute
    download URL. Scope is `lcpl_feed_download` (multi-use peek, 30 min)
    when the serializer context flags an OPDS feed render; otherwise
    `lcpl_download` (single-use, 60s).

    Falls back to the relative path with no token when no request is
    in context — that response is unusable by clients but is fine for
    operator scripts and tests.
    """
    relative = reverse("readium:license-gateway", kwargs={"license_id": license_id})
    context = info.context or {}
    request = context.get("request")
    if request is None:
        return relative

    if context.get("opds_feed"):
        scope = LCPL_FEED_DOWNLOAD
        ttl = lcpl_feed_download_ttl()
        single_use = False
    else:
        scope = LCPL_DOWNLOAD
        ttl = lcpl_download_ttl()
        single_use = True

    token = CapabilityTokenService.mint(
        scope=scope,
        subject={"sub": str(user_id), "resource_id": str(license_id)},
        ttl=ttl,
        single_use=single_use,
    )
    return request.build_absolute_uri(f"{relative}?token={token}")


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
        # IP-009 Phase 5: per-loan renewal history.
        renewal_count: int = 0

        # IP-009 Phase 4: `download_url` carries a capability token
        # minted inline. Computed *after* the row fields so the
        # validator can read `id` and `user_id` from `info.data`.
        download_url: str = Field(default="", validate_default=True)

        @field_validator("download_url", mode="before")
        def _build_download_url(cls, v, info: ValidationInfo) -> str:
            license_id = info.data.get("id")
            user_id = info.data.get("user_id")
            if license_id is None or user_id is None:
                return v or ""
            return _mint_lcpl_download_url(license_id, user_id, info)

        @computed_field
        @property
        def reader_requirements(self) -> dict:
            return {
                "reader": "Thorium Reader",
                "reader_url": "https://www.edrlab.org/software/thorium-reader/",
                "note": "LCP-protected publications can only be opened in an LCP-compliant reader.",
            }

        @computed_field
        @property
        def renewals_remaining(self) -> Optional[int]:
            """
            IP-009 Phase 5 (Q6): None when `EVILFLOWERS_READIUM_MAX_RENEWALS`
            is unset (uncapped); otherwise `max(0, cap - renewal_count)`.
            """
            cap = getattr(settings, "EVILFLOWERS_READIUM_MAX_RENEWALS", None)
            if cap is None:
                return None
            return max(0, cap - self.renewal_count)

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
