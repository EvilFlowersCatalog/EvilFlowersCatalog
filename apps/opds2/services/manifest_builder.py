from typing import Optional

from django.urls import reverse

from apps.core.fields.localized import localize
from apps.core.models import Entry
from apps.opds.services.borrow_link import (
    PROTECTED_MEDIA_TYPES,
    BorrowLink,
    BorrowLinkResolver,
    LendingState,
    lendable_acquisition,
)
from apps.opds.services.identifiers import primary_identifier
from apps.opds2.schema.opds import Availability, Copies, Holds
from apps.opds2.schema.rwpm import Contributor, Link, Metadata, Properties, Publication, Subject
from apps.readium.models import EncryptedContent

OPDS_PUBLICATION_JSON = "application/opds-publication+json"
SCHEMA_ORG_BOOK = "http://schema.org/Book"


class ManifestBuilder:
    """Converts Django Entry models to RWPM Publications and OPDS 2.0 publication objects."""

    OPDS_REL_ACQUISITION = "http://opds-spec.org/acquisition"
    OPDS_REL_OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"
    OPDS_REL_BORROW = "http://opds-spec.org/acquisition/borrow"
    OPDS_REL_COVER = "http://opds-spec.org/image"
    OPDS_REL_THUMBNAIL = "http://opds-spec.org/image/thumbnail"

    @staticmethod
    def base_url(request) -> str:
        return f"{request.scheme}://{request.get_host()}" if request is not None else ""

    @classmethod
    def build_publication(
        cls,
        entry: Entry,
        request=None,
        *,
        user=None,
        lending: Optional[LendingState] = None,
    ) -> Publication:
        """An OPDS 2.0 publication, as `user` sees it.

        For an LCP title the acquisition links depend on the user: a borrow
        link (with copies, holds and availability) or, when the user holds a
        loan, the `.lcpl` link with the loan period. `lending` lets a caller
        that has just changed the loan state pass it in instead of re-reading.
        """
        base_url = cls.base_url(request)
        links = [
            Link(
                href=f"{base_url}{reverse('opds2:publication-detail', kwargs={'catalog_name': entry.catalog.url_name, 'entry_id': entry.pk})}",
                type=OPDS_PUBLICATION_JSON,
                rel="self",
            )
        ]
        images = cls._build_cover_links(entry, base_url)

        if entry.read_config("readium_enabled"):
            resolver = BorrowLinkResolver(request, opds_version="2.0")
            links.extend(cls.from_borrow_link(link) for link in resolver.emit_links(entry, user, lending))
        else:
            links.extend(cls._build_acquisition_links(entry, base_url))

        return Publication(
            metadata=cls.build_metadata(entry),
            links=links,
            images=images if images else None,
        )

    @classmethod
    def build_manifest(cls, entry: Entry, base_url: str = "") -> Publication:
        """Build a standalone RWPM manifest for a publication (used by LCP readers).

        For an LCP title the reading order points at the encrypted
        publication, never at the raw file.
        """
        metadata = cls.build_metadata(entry)
        links = [
            Link(
                href=f"{base_url}{reverse('opds2:publication-manifest', kwargs={'catalog_name': entry.catalog.url_name, 'entry_id': entry.pk})}",
                type="application/webpub+json",
                rel="self",
            )
        ]
        images = cls._build_cover_links(entry, base_url)

        reading_order = []
        if entry.read_config("readium_enabled"):
            protected = cls._encrypted_reading_order(entry, base_url)
            if protected is not None:
                reading_order.append(protected)
        else:
            for acquisition in entry.acquisitions.all():
                reading_order.append(
                    Link(
                        href=f"{base_url}{reverse('files:acquisition-download', kwargs={'acquisition_id': acquisition.pk})}",
                        type=acquisition.mime,
                    )
                )

        return Publication(
            context=["https://readium.org/webpub-manifest/context.jsonld"],
            metadata=metadata,
            links=links,
            reading_order=reading_order if reading_order else None,
            resources=images or None,
        )

    @classmethod
    def build_metadata(cls, entry: Entry) -> Metadata:
        authors = cls._build_contributors(entry)
        subjects = cls._build_subjects(entry)

        language = None
        if entry.language:
            language = [entry.language.alpha2]

        publisher = None
        if entry.publisher:
            publisher = [Contributor(name=entry.publisher)]

        published = None
        if entry.published_at:
            published = str(entry.published_at)

        return Metadata(
            type=SCHEMA_ORG_BOOK,
            # Readers key an imported publication on its identifier; fall back
            # to the entry's UUID so every publication has a stable one.
            identifier=primary_identifier(entry),
            title=entry.title,
            author=authors if authors else None,
            publisher=publisher,
            language=language,
            subject=subjects if subjects else None,
            description=entry.summary,
            published=published,
            modified=entry.updated_at,
            number_of_pages=entry.page_count or None,
        )

    @staticmethod
    def from_borrow_link(link: BorrowLink) -> Link:
        """JSON form of a `BorrowLink` (apps/opds/services/borrow_link.py)."""
        indirect = None
        for media_type in reversed(link.indirect):
            indirect = [Link(type=media_type, children=indirect)]

        properties = Properties(
            indirect_acquisition=indirect,
            availability=(
                Availability(
                    state=link.availability_state,
                    since=link.availability_since,
                    until=link.availability_until,
                )
                if link.availability_state
                else None
            ),
            copies=(
                Copies(total=link.copies_total, available=link.copies_available)
                if link.copies_total is not None
                else None
            ),
            holds=(
                Holds(total=link.holds_total, position=link.holds_position) if link.holds_total is not None else None
            ),
        )
        return Link(href=link.href, type=link.type, rel=link.rel, title=link.title, properties=properties)

    @classmethod
    def _build_contributors(cls, entry: Entry) -> list[Contributor]:
        contributors = []
        # Sort in Python so a prefetched `entry_authors__author` is reused.
        for entry_author in sorted(entry.entry_authors.all(), key=lambda ea: ea.position):
            author = entry_author.author
            contributors.append(
                Contributor(
                    name=author.full_name,
                    sort_as=f"{author.surname}, {author.name}" if author.surname else None,
                )
            )
        return contributors

    @classmethod
    def _build_subjects(cls, entry: Entry) -> list[Subject]:
        subjects = []
        for category in entry.categories.all():
            subjects.append(
                Subject(
                    name=localize(category.label) or category.term,
                    scheme=category.scheme,
                    code=category.term,
                )
            )
        return subjects

    @classmethod
    def _build_cover_links(cls, entry: Entry, base_url: str) -> list[Link]:
        links = []
        if entry.image:
            links.append(
                Link(
                    href=f"{base_url}{entry.image_url}",
                    type=entry.image_mime or "image/jpeg",
                    rel=cls.OPDS_REL_COVER,
                )
            )
        if entry.thumbnail:
            links.append(
                Link(
                    href=f"{base_url}{entry.thumbnail_url}",
                    type=entry.thumbnail_mime or entry.image_mime or "image/jpeg",
                    rel=cls.OPDS_REL_THUMBNAIL,
                )
            )
        return links

    @classmethod
    def _build_acquisition_links(cls, entry: Entry, base_url: str) -> list[Link]:
        """Plain acquisition links of a non-LCP entry."""
        links = []
        for acquisition in entry.acquisitions.all():
            links.append(
                Link(
                    href=f"{base_url}{reverse('files:acquisition-download', kwargs={'acquisition_id': acquisition.pk})}",
                    type=acquisition.mime,
                    rel=cls._acquisition_type_to_rel(acquisition.relation),
                )
            )
        return links

    @staticmethod
    def _encrypted_reading_order(entry: Entry, base_url: str) -> Optional[Link]:
        acquisition = lendable_acquisition(entry)
        if acquisition is None:
            return None
        try:
            encrypted = acquisition.encrypted_content
        except EncryptedContent.DoesNotExist:
            return None
        return Link(
            href=f"{base_url}{reverse('readium:encrypted-content-download', kwargs={'lcp_content_id': encrypted.lcp_content_id})}",
            type=PROTECTED_MEDIA_TYPES.get(acquisition.mime, acquisition.mime),
        )

    @classmethod
    def _acquisition_type_to_rel(cls, relation: str) -> str:
        mapping = {
            "open-access": cls.OPDS_REL_OPEN_ACCESS,
            "borrow": cls.OPDS_REL_BORROW,
            "acquisition": cls.OPDS_REL_ACQUISITION,
        }
        return mapping.get(relation, cls.OPDS_REL_ACQUISITION)
