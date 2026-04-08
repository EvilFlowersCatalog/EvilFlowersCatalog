from django.conf import settings
from django.urls import reverse

from apps.core.models import Entry
from apps.opds2.schema.opds import Availability, Copies
from apps.opds2.schema.rwpm import Contributor, Link, Metadata, Properties, Publication, Subject
from apps.readium.models import EncryptedContent, License


class ManifestBuilder:
    """Converts Django Entry models to RWPM Publications and OPDS 2.0 publication objects."""

    OPDS_REL_ACQUISITION = "http://opds-spec.org/acquisition"
    OPDS_REL_OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"
    OPDS_REL_BORROW = "http://opds-spec.org/acquisition/borrow"
    OPDS_REL_COVER = "http://opds-spec.org/image"
    OPDS_REL_THUMBNAIL = "http://opds-spec.org/image/thumbnail"

    LCP_LICENSE_TYPE = "application/vnd.readium.lcp.license.v1.0+json"

    @classmethod
    def build_publication(cls, entry: Entry, base_url: str = "", include_availability: bool = True) -> Publication:
        metadata = cls.build_metadata(entry)
        links = [
            Link(
                href=f"{base_url}{reverse('opds2:publication-detail', kwargs={'catalog_name': entry.catalog.url_name, 'entry_id': entry.pk})}",
                type="application/opds+json",
                rel="self",
            )
        ]
        images = cls._build_cover_links(entry, base_url)
        acquisition_links = cls._build_acquisition_links(entry, base_url, include_availability)

        return Publication(
            metadata=metadata,
            links=links + acquisition_links,
            images=images if images else None,
        )

    @classmethod
    def build_manifest(cls, entry: Entry, base_url: str = "") -> Publication:
        """Build a standalone RWPM manifest for a publication (used by LCP readers)."""
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
            resources=images,
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

        identifier = None
        if entry.identifiers:
            if "isbn" in entry.identifiers:
                identifier = f"urn:isbn:{entry.identifiers['isbn']}"
            elif "doi" in entry.identifiers:
                identifier = f"https://doi.org/{entry.identifiers['doi']}"

        published = None
        if entry.published_at:
            published = str(entry.published_at)

        return Metadata(
            identifier=identifier,
            title=entry.title,
            author=authors if authors else None,
            publisher=publisher,
            language=language,
            subject=subjects if subjects else None,
            description=entry.summary,
            published=published,
            modified=entry.updated_at if hasattr(entry, "updated_at") else None,
        )

    @classmethod
    def _build_contributors(cls, entry: Entry) -> list[Contributor]:
        contributors = []
        for entry_author in entry.entry_authors.select_related("author").order_by("position"):
            author = entry_author.author
            contributors.append(
                Contributor(
                    name=author.full_name,
                    sort_as=f"{author.surname}, {author.name}" if author.surname else None,
                    position=float(entry_author.position) if entry_author.position else None,
                )
            )
        return contributors

    @classmethod
    def _build_subjects(cls, entry: Entry) -> list[Subject]:
        subjects = []
        for category in entry.categories.all():
            subjects.append(
                Subject(
                    name=category.label or category.term,
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
                    href=f"{base_url}{reverse('files:cover-download', kwargs={'entry_id': entry.pk})}",
                    type=entry.image_mime or "image/jpeg",
                    rel=cls.OPDS_REL_COVER,
                )
            )
        if entry.thumbnail:
            links.append(
                Link(
                    href=f"{base_url}{reverse('files:thumbnail-download', kwargs={'entry_id': entry.pk})}",
                    type=entry.image_mime or "image/jpeg",
                    rel=cls.OPDS_REL_THUMBNAIL,
                )
            )
        return links

    @classmethod
    def _build_acquisition_links(cls, entry: Entry, base_url: str, include_availability: bool = True) -> list[Link]:
        links = []

        if entry.read_config("readium_enabled"):
            link = cls._build_borrow_link(entry, base_url, include_availability)
            if link:
                links.append(link)
        else:
            for acquisition in entry.acquisitions.all():
                rel = cls._acquisition_type_to_rel(acquisition.relation)
                links.append(
                    Link(
                        href=f"{base_url}{reverse('files:acquisition-download', kwargs={'acquisition_id': acquisition.pk})}",
                        type=acquisition.mime,
                        rel=rel,
                    )
                )
        return links

    @classmethod
    def _build_borrow_link(cls, entry: Entry, base_url: str, include_availability: bool) -> Link | None:
        acquisition = entry.acquisitions.filter(mime__in=["application/epub+zip", "application/pdf"]).first()
        if not acquisition:
            return None

        properties = None
        if include_availability:
            max_concurrent = entry.read_config("readium_amount") or 1
            active_count = License.objects.filter(
                entry=entry,
                state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            ).count()
            available = max_concurrent - active_count

            # Check encryption readiness
            is_ready = False
            try:
                ec = acquisition.encrypted_content
                is_ready = ec.status == EncryptedContent.EncryptionStatus.REGISTERED
            except EncryptedContent.DoesNotExist:
                pass

            state = "available" if (available > 0 and is_ready) else "unavailable"

            properties = Properties(
                availability=Availability(state=state),
                copies=Copies(total=max_concurrent, available=max(0, available)),
                indirect_acquisition=[Link(type=acquisition.mime)],
            )

        return Link(
            href=f"{base_url}{reverse('opds2:borrow', kwargs={'catalog_name': entry.catalog.url_name, 'entry_id': entry.pk})}",
            type=cls.LCP_LICENSE_TYPE,
            rel=cls.OPDS_REL_BORROW,
            properties=properties,
            children=[Link(type=acquisition.mime)] if not properties else None,
        )

    @classmethod
    def _acquisition_type_to_rel(cls, relation: str) -> str:
        mapping = {
            "open-access": cls.OPDS_REL_OPEN_ACCESS,
            "borrow": cls.OPDS_REL_BORROW,
            "acquisition": cls.OPDS_REL_ACQUISITION,
        }
        return mapping.get(relation, cls.OPDS_REL_ACQUISITION)
