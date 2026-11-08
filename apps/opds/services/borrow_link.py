"""
BorrowLinkResolver — shared OPDS borrow / LCP-license link emission.

IP-008 Phase 2 B5 (Q4 resolution): the link tuple (rel, type, target URL)
for an LCP-protected entry is a domain concern that both OPDS 1.2 (Atom)
and OPDS 2.0 (RWPM JSON) need. Format conversion (XML `<link>` element
vs JSON link object) stays in the schema layer; the SEMANTICS live
here.

Reading-app contract (verified against Thorium Reader `develop`):

- A borrow link is dereferenced with GET, and Thorium only fetches it when
  its `type` is `application/opds-publication+json`; any other type is
  handed to the system web browser. So the borrow link advertises an OPDS
  publication, and the LCP license sits one level down in
  `indirectAcquisition` (license → protected publication).
- The publication returned by the borrow endpoint carries a plain
  `rel=acquisition` link typed as an LCP license; Thorium downloads and
  imports that.
- `properties.availability.state` drives the button: `unavailable` relabels
  it "place hold", `reserved` hides it.

Consumers:
- `apps/opds/schema.py::AcquisitionEntry.from_model` (OPDS 1.2)
- `apps/opds2/services/manifest_builder.py::ManifestBuilder` (OPDS 2.0)
- `apps/opds2/views/borrow.py::BorrowView`
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Literal, Optional

from django.urls import reverse
from django.utils import timezone

from apps.core.models import Acquisition, Entry
from apps.core.services.capability_tokens import CapabilityTokenService
from apps.readium.capability_scopes import LCPL_FEED_DOWNLOAD, lcpl_feed_download_ttl
from apps.readium.models import EncryptedContent, License, Reservation

LCP_LICENSE_MIME = "application/vnd.readium.lcp.license.v1.0+json"
OPDS_PUBLICATION_MIME = "application/opds-publication+json"
REL_BORROW = "http://opds-spec.org/acquisition/borrow"
REL_ACQUISITION = "http://opds-spec.org/acquisition"

LICENSED_STATES = (License.LicenseState.READY, License.LicenseState.ACTIVE)

# Same preference order as `LicenseService.create_license` (PDF first), so the
# advertised format is the one the license will actually protect.
LENDABLE_MIMES = (Acquisition.AcquisitionMIME.PDF, Acquisition.AcquisitionMIME.EPUB)

# Media type of the LCP-protected resource the license unlocks (Readium LCP
# profiles): an LCP PDF is `application/pdf+lcp`, an LCP EPUB keeps its type.
# The encrypted-content endpoint serves the same types.
PROTECTED_MEDIA_TYPES = {
    Acquisition.AcquisitionMIME.PDF: "application/pdf+lcp",
    Acquisition.AcquisitionMIME.EPUB: "application/epub+zip",
}


@dataclass(frozen=True)
class BorrowLink:
    """A protocol-neutral link description.

    OPDS 1.2 and OPDS 2.0 serializers each turn this into their
    profile-specific representation. `indirect` is the chain of media types
    behind the link, outermost first (OPDS `indirectAcquisition`).
    """

    rel: str
    href: str
    type: str
    title: Optional[str] = None
    indirect: tuple = ()
    availability_state: Optional[str] = None
    availability_since: Optional[datetime] = None
    availability_until: Optional[datetime] = None
    copies_total: Optional[int] = None
    copies_available: Optional[int] = None
    holds_total: Optional[int] = None
    holds_position: Optional[int] = None


@dataclass
class LendingState:
    """Everything an OPDS link needs to describe one LCP entry for one user."""

    protected_type: Optional[str]
    ready: bool
    total: int
    available: int
    holds_total: int
    next_return: Optional[datetime] = None
    license: Optional[License] = None
    reservation: Optional[Reservation] = None
    loans: List[License] = field(default_factory=list)

    @property
    def availability_state(self) -> str:
        if self.reservation is not None:
            return "ready" if self.reservation.status == Reservation.Status.AVAILABLE else "reserved"
        return "available" if (self.ready and self.available > 0) else "unavailable"


def lendable_acquisition(entry: Entry) -> Optional[Acquisition]:
    """The acquisition `LicenseService.create_license` would protect."""
    acquisitions = sorted(entry.acquisitions.all(), key=lambda a: a.created_at)
    for mime in LENDABLE_MIMES:
        for acquisition in acquisitions:
            if acquisition.mime == mime:
                return acquisition
    return None


class BorrowLinkResolver:
    """Computes the links to advertise for a readium-enabled entry.

    The resolver knows the URL routes — neither the OPDS 1.2 schema
    classes nor the OPDS 2.0 manifest builder should be reversing
    LCP-borrow URLs by hand.
    """

    def __init__(self, request, *, opds_version: Literal["1.2", "2.0"] = "2.0"):
        self.request = request
        self.opds_version = opds_version
        self._base_url = f"{request.scheme}://{request.get_host()}" if request is not None else ""

    def borrow_url(self, entry: Entry) -> str:
        """The loan resource of an entry: GET borrows (or reports the loan).

        Both OPDS profiles point at the OPDS 2.0 borrow endpoint
        (cross-version link). Readers follow links by rel + type, not by
        OPDS version.
        """
        path = reverse(
            "opds2:borrow",
            kwargs={"catalog_name": entry.catalog.url_name, "entry_id": entry.id},
        )
        return f"{self._base_url}{path}"

    def license_url(self, license: License) -> str:
        """Direct URL to the .lcpl license document.

        After IP-009 Phase 4 the gateway is capability-token only.
        OPDS feed responses carry a `lcpl_feed_download`-scoped token
        (multi-use peek within TTL) so the reader app can fetch the
        `.lcpl` without an extra mint round-trip.
        """
        path = reverse("readium:license-gateway", kwargs={"license_id": license.pk})
        user = getattr(self.request, "user", None) if self.request is not None else None
        if user is None or not getattr(user, "is_authenticated", False):
            # Anonymous OPDS render — no user to scope to. Return the
            # tokenless URL so the unauthenticated client at least sees
            # a stable shape; the gateway will 401 on consume.
            return f"{self._base_url}{path}"
        token = CapabilityTokenService.mint(
            scope=LCPL_FEED_DOWNLOAD,
            subject={"sub": str(user.pk), "resource_id": str(license.pk)},
            ttl=lcpl_feed_download_ttl(),
            single_use=False,
        )
        return f"{self._base_url}{path}?token={token}"

    @staticmethod
    def lending_state(entry: Entry, user=None) -> LendingState:
        """Loans, holds and encryption readiness of `entry`, seen by `user`.

        Two queries (loans, holds) plus the encryption status of the lendable
        acquisition. A promoted-but-unclaimed reservation holds a slot, as in
        `ReservationService._slots_in_use`.
        """
        now = timezone.now()
        acquisition = lendable_acquisition(entry)

        ready = False
        if acquisition is not None:
            try:
                ready = acquisition.encrypted_content.status == EncryptedContent.EncryptionStatus.REGISTERED
            except EncryptedContent.DoesNotExist:
                ready = False

        loans = list(License.objects.filter(entry=entry, state__in=LICENSED_STATES, expires_at__gt=now))
        reservations = list(
            Reservation.objects.filter(entry=entry, status__in=Reservation.NON_TERMINAL_STATUSES).order_by("position")
        )

        total = int(entry.read_config("readium_amount") or 0)
        promoted = sum(1 for r in reservations if r.status == Reservation.Status.AVAILABLE)
        user_id = user.pk if user is not None and getattr(user, "is_authenticated", False) else None

        return LendingState(
            protected_type=PROTECTED_MEDIA_TYPES.get(acquisition.mime) if acquisition else None,
            ready=ready,
            total=total,
            available=max(0, total - len(loans) - promoted),
            holds_total=sum(1 for r in reservations if r.status == Reservation.Status.QUEUED),
            next_return=min((loan.expires_at for loan in loans), default=None),
            license=next((loan for loan in loans if user_id and loan.user_id == user_id), None),
            reservation=next((r for r in reservations if user_id and r.user_id == user_id), None),
            loans=loans,
        )

    def emit_links(self, entry: Entry, user=None, state: Optional[LendingState] = None) -> List[BorrowLink]:
        """Build the LCP-related links for an entry, as seen by `user`.

        - The user holds a loan → one `rel=acquisition` link to the `.lcpl`,
          with the loan period as availability. No borrow link: the loan is
          returned and renewed through the License Status Document.
        - Otherwise → one `rel=borrow` link to the loan resource, typed as an
          OPDS publication, carrying copies, holds and availability
          (`available` / `unavailable` / `reserved` / `ready`).

        The direct download link for the underlying file is intentionally
        NOT returned: for readium-enabled entries the LCP-protected delivery
        is the contract; emitting the raw download alongside leaks the file.
        """
        if not entry.read_config("readium_enabled"):
            return []

        if state is None:
            state = self.lending_state(entry, user)
        protected = (state.protected_type,) if state.protected_type else ()

        if state.license is not None:
            return [
                BorrowLink(
                    rel=REL_ACQUISITION,
                    href=self.license_url(state.license),
                    type=LCP_LICENSE_MIME,
                    title="Download license",
                    indirect=protected,
                    availability_state="available",
                    availability_since=state.license.starts_at,
                    availability_until=state.license.expires_at,
                )
            ]

        since = until = None
        position = None
        availability_state = state.availability_state
        if state.reservation is not None:
            since = state.reservation.available_at or state.reservation.requested_at
            if state.reservation.status == Reservation.Status.AVAILABLE:
                until = state.reservation.claim_deadline
            else:
                position = state.reservation.position
        elif availability_state == "unavailable" and state.ready:
            until = state.next_return

        return [
            BorrowLink(
                rel=REL_BORROW,
                href=self.borrow_url(entry),
                type=OPDS_PUBLICATION_MIME,
                title="Borrow",
                indirect=(LCP_LICENSE_MIME, *protected),
                availability_state=availability_state,
                availability_since=since,
                availability_until=until,
                copies_total=state.total,
                copies_available=state.available,
                holds_total=state.holds_total,
                holds_position=position,
            )
        ]
