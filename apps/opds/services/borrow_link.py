"""
BorrowLinkResolver — shared OPDS borrow / LCP-license link emission.

IP-008 Phase 2 B5 (Q4 resolution): the link tuple (rel, type, target URL)
for an LCP-protected entry is a domain concern that both OPDS 1.2 (Atom)
and OPDS 2.0 (RWPM JSON) need. Format conversion (XML `<link>` element
vs JSON link object) stays in the schema layer; the SEMANTICS live
here.

Consumers:
- `apps/opds/schema.py::AcquisitionEntry.from_model` (OPDS 1.2)
- `apps/opds2/views/borrow.py::BorrowView`
- `apps/opds2/services/feed_builder.py::FeedBuilder.build_shelf_feed`
"""

from dataclasses import dataclass
from typing import List, Literal, Optional

from django.urls import reverse

from apps.core.models import Entry
from apps.readium.models import License

LCP_LICENSE_MIME = "application/vnd.readium.lcp.license.v1.0+json"
REL_BORROW = "http://opds-spec.org/acquisition/borrow"
REL_ACQUISITION = "http://opds-spec.org/acquisition"


@dataclass(frozen=True)
class BorrowLink:
    """A protocol-neutral link description.

    OPDS 1.2 and OPDS 2.0 serializers each turn this into their
    profile-specific representation.
    """

    rel: str
    href: str
    type: str
    title: Optional[str] = None


class BorrowLinkResolver:
    """Computes the (rel, type, href) tuples to advertise for an entry.

    The resolver knows the URL routes — neither the OPDS 1.2 schema
    classes nor the OPDS 2.0 manifest builder should be reversing
    LCP-borrow URLs by hand.
    """

    def __init__(self, request, *, opds_version: Literal["1.2", "2.0"] = "2.0"):
        self.request = request
        self.opds_version = opds_version
        self._base_url = f"{request.scheme}://{request.get_host()}" if request is not None else ""

    def borrow_url(self, entry: Entry) -> str:
        """The URL a reader app POSTs to in order to obtain a license.

        Both OPDS profiles point at the OPDS 2.0 borrow endpoint
        (cross-version link). Thorium and other Readium-toolkit readers
        follow links by rel + type, not by OPDS version.
        """
        path = reverse(
            "opds2:borrow",
            kwargs={"catalog_name": entry.catalog.url_name, "entry_id": entry.id},
        )
        return f"{self._base_url}{path}"

    def license_url(self, license: License) -> str:
        """Direct URL to the .lcpl license document."""
        path = reverse("readium:license-gateway", kwargs={"license_id": license.pk})
        return f"{self._base_url}{path}"

    def emit_links(self, entry: Entry, active_license: Optional[License] = None) -> List[BorrowLink]:
        """Build the LCP-related link tuples for an entry.

        - Borrow link (rel=borrow, type=lcp-license-json) → OPDS 2.0
          borrow endpoint. Emitted iff `entry.read_config('readium_enabled')`.
        - Direct acquisition (rel=acquisition, type=lcp-license-json) →
          `.lcpl` for the active license, if the user has one.

        The direct download link (rel=acquisition, type=mime) for the
        underlying file is intentionally NOT returned here: for
        readium-enabled entries the LCP-protected delivery is the
        contract; emitting the raw download alongside leaks the file.
        Callers can still emit non-LCP acquisitions for non-readium
        entries via their existing schema logic.
        """
        links: List[BorrowLink] = []

        if not entry.read_config("readium_enabled"):
            return links

        links.append(
            BorrowLink(
                rel=REL_BORROW,
                href=self.borrow_url(entry),
                type=LCP_LICENSE_MIME,
                title="Borrow",
            )
        )

        if active_license is not None:
            links.append(
                BorrowLink(
                    rel=REL_ACQUISITION,
                    href=self.license_url(active_license),
                    type=LCP_LICENSE_MIME,
                    title="Download license",
                )
            )

        return links

    @staticmethod
    def active_license_for(entry: Entry, user) -> Optional[License]:
        """Returns the user's READY/ACTIVE license on this entry, if any."""
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return License.objects.filter(
            entry=entry,
            user=user,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
        ).first()
