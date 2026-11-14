from datetime import datetime
from typing import Optional
from uuid import UUID

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
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

_LOOPBACK_MARKERS = ("://127.0.0.1", "://localhost", "://0.0.0.0", "://[::1]")


def _public_base_url() -> str:
    """The configured public origin, or "" when it is unset or loopback.

    Behind the k8s edge gateway the request reaches Django as plain http, so
    `request.build_absolute_uri` yields an `http://` link that browsers block
    on the https portal (mixed content). The configured origin is the one
    clients actually use, same as the e-mailed `.lcpl` links.
    """
    base_url = str(getattr(settings, "EVILFLOWERS_BASE_URL", "") or "").rstrip("/")
    if not base_url or any(marker in base_url for marker in _LOOPBACK_MARKERS):
        return ""
    return base_url


def _mint_lcpl_download_url(license_id: UUID, user_id: UUID, info: ValidationInfo) -> str:
    """
    IP-009 Phase 4 D2: mint a capability token and return the absolute
    download URL. Scope is `lcpl_feed_download` (30 min) when the
    serializer context flags an OPDS feed render; otherwise
    `lcpl_download` (short TTL).

    Both scopes are redeemable more than once within their TTL.
    Single-use is unusable here: reading apps fetch the `.lcpl` URL
    twice — Thorium issues a throwaway GET purely to sniff
    `Content-Type`, then fetches again for the real download (see
    `importFromLinkService` in edrlab/thorium-reader). A single-use
    token is burned by the sniff, so the real download 401s and the
    import fails with "publicationDocument not imported on db".

    Replay exposure stays bounded by the TTL, and the `.lcpl` itself is
    inert without the user's passphrase — the LCP model assumes the
    licence document may be copied around.

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
    else:
        scope = LCPL_DOWNLOAD
        ttl = lcpl_download_ttl()
    single_use = False

    token = CapabilityTokenService.mint(
        scope=scope,
        subject={"sub": str(user_id), "resource_id": str(license_id)},
        ttl=ttl,
        single_use=single_use,
    )
    path = f"{relative}?token={token}"
    public_base = _public_base_url()
    if public_base:
        return f"{public_base}{path}"
    return request.build_absolute_uri(path)


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
        def is_active_loan(self) -> bool:
            """True while the user can read the publication.

            `ready` is a live loan that has not been opened in a reader yet —
            LSD flips it to `active` on the first device registration. The
            portal filtered "my loans" on `state == "active"` and hid every
            never-opened loan (the reader saw "no active loans" while the copy
            was blocked for 14 days). Clients should use this flag, or
            `GET /licenses?active=true`, instead of comparing `state`.
            """
            if self.state not in (License.LicenseState.READY, License.LicenseState.ACTIVE):
                return False
            return self.expires_at is None or self.expires_at > timezone.now()

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
        def renew_policy(self) -> dict:
            """The renewal rules, published so clients stop guessing them.

            The portal previously hardcoded its own extension options and had no
            way to know the server's bounds, so it offered choices the API always
            refused (`requested_end` is capped at `now + max_renew_days`) and
            could not explain the embargo to the user. Publishing the policy keeps
            one source of truth: `evaluate_renew` and the UI read the same numbers.

            `embargo_days` is an upper bound: the embargo is capped at half the
            loan. Use `renewable_from` for this loan's actual date (#110).
            """
            return {
                "max_renew_days": settings.EVILFLOWERS_READIUM_MAX_RENEW_DAYS,
                "embargo_days": settings.EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS,
                "max_renewals": getattr(settings, "EVILFLOWERS_READIUM_MAX_RENEWALS", None),
            }

        @computed_field
        @property
        def renewable_from(self) -> Optional[datetime]:
            """When the renewal embargo lifts for this loan; `None` if it does not apply.

            Computed by the same helper `evaluate_renew` uses, so a client that
            disables "renew" until this moment never offers a renewal the API
            refuses for the embargo — and never withholds one it would allow.
            Other denials (queue, cap) still come back as a 403 with a `code`.
            """
            from apps.readium.services.renew_policy import renewable_from

            return renewable_from(self.starts_at, self.expires_at, self.renewal_count)

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

    class Detailed(Base):
        """Reservation list/detail for the portal: embeds the entry so the queue
        UI can render title/author/thumbnail without an N+1 fetch per row."""

        entry: EntrySerializer.Base


class EncryptionJobSerializer:
    """LCP encryption jobs (#148): the MCP `ENCRYPTION_JOB_SCHEMA`, plus the ledger link and `stuck`."""

    class Job(Serializer):
        content_id: str = Field(description="LCP content id; pass it to requeue / reencrypt.")
        status: str = Field(description="pending, completed, failed or registered.")
        entry_id: UUID
        entry_title: str
        catalog: str = Field(description="Catalog `url_name`.")
        mime: str
        input_path: str
        encrypted_path: str
        error_message: Optional[str]
        created_at: datetime
        updated_at: datetime
        encrypted_at: Optional[datetime]
        task_id: Optional[str] = Field(description="Celery task id of the last dispatch.")
        trace_id: Optional[str]
        attempts: int
        last_dispatched_at: Optional[datetime]
        reported_at: Optional[datetime]
        report: Optional[dict]
        stale: bool = Field(description="pending, no report, past EVILFLOWERS_READIUM_ENCRYPTION_STALE_MINUTES.")
        stuck: bool = Field(description="pending for over an hour: the dashboard's `attention.encryption_stuck`.")
        readium_enabled: bool = Field(description="LCP is still switched on for the entry.")
        job_id: Optional[UUID] = Field(description="The file's `readium.encrypt` job in /api/v1/jobs.")

        @staticmethod
        def view(encrypted_content, job_id=None) -> dict:
            from apps.readium.services.encryption_jobs import encryption_job, is_stuck

            view = encryption_job(encrypted_content)
            view["stuck"] = is_stuck(encrypted_content)
            view["readium_enabled"] = bool(encrypted_content.acquisition.entry.read_config("readium_enabled"))
            view["job_id"] = job_id
            return view

    class EntryJob(Job):
        """The job plus the two fields the entry endpoint has always returned."""

        lcp_content_id: str
        ready_for_licensing: bool

    class EntryFile(Serializer):
        acquisition_id: UUID
        mime: str
        encryption: Optional["EncryptionJobSerializer.EntryJob"]

    class EntryStatus(Serializer):
        """`GET /readium/v1/entries/{id}/encryption`."""

        readium_enabled: bool
        has_acquisition: Optional[bool] = None
        encryption: Optional["EncryptionJobSerializer.EntryJob"] = Field(default=None, description="The first file's.")
        acquisitions: list["EncryptionJobSerializer.EntryFile"] = []
        message: Optional[str] = None

    class EntryTriggered(Serializer):
        """`POST /readium/v1/entries/{id}/encryption`."""

        message: str
        lcp_content_id: str = Field(description="The first file's.")
        status: str
        jobs: list[dict]


EncryptionJobSerializer.EntryFile.model_rebuild()
EncryptionJobSerializer.EntryStatus.model_rebuild()
