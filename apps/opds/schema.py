import datetime
from enum import Enum
from typing import Optional, List, Union, Literal, Dict

from django.urls import reverse
from pydantic import EmailStr, Field
from pydantic_xml import BaseXmlModel, attr, element

from apps.core.fields.localized import localize
from apps.core.models import Entry, Acquisition
from apps.opds.services.identifiers import entry_identifier_uris

NSMAP = {
    "": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "opds": "http://opds-spec.org/2010/catalog",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "ef": "http://elvira.dital/schema/evilflowers-opds.xsd",
    # RFC 5005 (Feed Paging and Archiving) — `fh:complete` marks an unpaged
    # Complete Acquisition Feed (OPDS 1.2 §2.5).
    "fh": "http://purl.org/syndication/history/1.0",
}


NAVIGATION_FEED_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQUISITION_FEED_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
AUTHENTICATION_DOCUMENT_TYPE = "application/opds-authentication+json"


def feed_type(kind: str) -> str:
    """Media type of an OPDS 1.2 catalog feed of `kind` (navigation/acquisition)."""
    return f"application/atom+xml;profile=opds-catalog;kind={kind}"


class Author(BaseXmlModel, tag="author", nsmap=NSMAP):
    name: str = element()
    email: Optional[EmailStr] = element(default=None)
    uri: Optional[str] = element(default=None)  # TODO: URI URN field?


class Content(BaseXmlModel, tag="content", nsmap=NSMAP):
    type: str = attr()
    value: str


class Summary(BaseXmlModel, tag="summary", nsmap=NSMAP):
    type: str = attr()
    value: str


class Category(BaseXmlModel, tag="category", nsmap=NSMAP):
    # RFC 4287 §4.2.2: `term` (required), `scheme` and `label` are attributes
    # of an empty `atom:category` element.
    term: str = attr()
    scheme: Optional[str] = attr(default=None)
    label: Optional[str] = attr(default=None)


class LinkType(str, Enum):
    SELF = "self"
    START = "start"
    SEARCH = "search"
    UP = "up"
    RELATED = "related"
    ALTERNATE = "alternate"
    SUBSECTION = "subsection"
    POPULAR = "http://opds-spec.org/sort/popular"
    SHELF = "http://opds-spec.org/shelf"
    NEW = "http://opds-spec.org/sort/new"
    IMAGE = "http://opds-spec.org/image"
    THUMBNAIL = "http://opds-spec.org/image/thumbnail"
    # OPDS 1.2 §2.5: a catalog links its Complete Acquisition Feed with this rel.
    CRAWLABLE = "http://opds-spec.org/crawlable"
    # Authentication for OPDS 1.0 §2.2: where to find the Authentication Document.
    AUTH_DOCUMENT = "http://opds-spec.org/auth/document"
    OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"
    ACQUISITION = "http://opds-spec.org/acquisition"
    # LCP borrow (IP-008): a readium-enabled entry advertises this rel so
    # OPDS 1.2 readers can reach the borrow endpoint. Without the member,
    # `BorrowLinkResolver` emitting `rel=…/acquisition/borrow` raises a
    # pydantic ValidationError and 500s every 1.2 feed that contains an
    # LCP title.
    BORROW = "http://opds-spec.org/acquisition/borrow"
    # RFC 5005 §3 paged-feed relations (OPDS 1.2 §2.4). Same closed-enum
    # trap as BORROW: a missing member 500s every paged feed.
    FIRST = "first"
    PREVIOUS = "previous"
    NEXT = "next"
    LAST = "last"


class IndirectAcquisition(BaseXmlModel, tag="indirectAcquisition", ns="opds", nsmap=NSMAP):
    """OPDS 1.2 §5.2.1 `opds:indirectAcquisition`: what an acquisition link
    leads to, nested outermost first (LCP license → protected publication)."""

    type: str = attr()
    children: List["IndirectAcquisition"] = element(tag="indirectAcquisition", ns="opds", default=list())

    @classmethod
    def chain(cls, types) -> Optional["IndirectAcquisition"]:
        node = None
        for media_type in reversed(list(types)):
            node = cls(type=media_type, children=[node] if node is not None else [])
        return node


IndirectAcquisition.model_rebuild()


class Availability(BaseXmlModel, tag="availability", ns="opds", nsmap=NSMAP):
    """Lending extension of OPDS 1.x (Library Simplified / Palace), the Atom
    counterpart of OPDS 2.0 `properties.availability`."""

    status: str = attr()
    since: Optional[datetime.datetime] = attr(default=None)
    until: Optional[datetime.datetime] = attr(default=None)


class Copies(BaseXmlModel, tag="copies", ns="opds", nsmap=NSMAP):
    total: Optional[int] = attr(default=None)
    available: Optional[int] = attr(default=None)


class Holds(BaseXmlModel, tag="holds", ns="opds", nsmap=NSMAP):
    total: Optional[int] = attr(default=None)
    position: Optional[int] = attr(default=None)


class Link(BaseXmlModel, tag="link", nsmap=NSMAP):
    rel: LinkType = attr()
    href: str = attr()
    type: str = attr()
    title: Optional[str] = attr(default=None)
    checksum: Optional[str] = attr(ns="ef", default=None)
    indirect_acquisition: Optional[IndirectAcquisition] = element(default=None)
    availability: Optional[Availability] = element(default=None)
    holds: Optional[Holds] = element(default=None)
    copies: Optional[Copies] = element(default=None)

    @classmethod
    def from_borrow_link(cls, link) -> "Link":
        """Atom form of a `BorrowLink` (apps/opds/services/borrow_link.py)."""
        return cls(
            rel=link.rel,
            href=link.href,
            type=link.type,
            title=link.title,
            indirect_acquisition=IndirectAcquisition.chain(link.indirect),
            availability=(
                Availability(
                    status=link.availability_state,
                    since=link.availability_since,
                    until=link.availability_until,
                )
                if link.availability_state
                else None
            ),
            holds=(
                Holds(total=link.holds_total, position=link.holds_position) if link.holds_total is not None else None
            ),
            copies=(
                Copies(total=link.copies_total, available=link.copies_available)
                if link.copies_total is not None
                else None
            ),
        )


class OpdsEntry(BaseXmlModel, nsmap=NSMAP):
    title: str = element()
    id: str = element()
    updated: datetime.datetime = element()
    links: List[Link] = element(tag="link", default=list())


class NavigationEntry(OpdsEntry, tag="entry"):
    author: Optional[Author] = element(default=None)
    content: Content = element()


class AcquisitionEntry(OpdsEntry, tag="entry"):
    authors: List[Author] = element(tag="author", default=list())
    # OPDS 1.2 §5.1: Dublin Core terms carry the bibliographic metadata.
    identifiers: List[str] = element(tag="identifier", ns="dc", default=list())
    language: Optional[str] = element(tag="language", ns="dc", default=None)
    publisher: Optional[str] = element(tag="publisher", ns="dc", default=None)
    issued: Optional[str] = element(tag="issued", ns="dc", default=None)
    summary: Summary = element()
    categories: Optional[List[Category]] = element(tag="category", default=list())
    content: Optional[Content] = element(default=None)

    @classmethod
    def from_model(
        cls,
        entry: Entry,
        complete: bool = False,
        *,
        request=None,
        user=None,
    ) -> "AcquisitionEntry":
        """Build an OPDS 1.2 `<entry>` from a domain `Entry`.

        When `request` is provided and the entry is readium-enabled,
        the LCP-related links (borrow + optional `.lcpl`) are emitted
        via `apps/opds/services/borrow_link.BorrowLinkResolver` so OPDS
        1.2 and OPDS 2.0 stay in sync at the link semantics layer
        (IP-008 Phase 2 B5). The raw download link is suppressed for
        LCP-protected entries to match OPDS 2.0 behaviour.
        """
        acquisition_entry = AcquisitionEntry(
            title=entry.title,
            id=f"urn:uuid:{entry.id}",
            updated=entry.updated_at,
            authors=[
                Author(name=entry_author.author.full_name)
                # Sort in Python (not `.order_by`) so a prefetched
                # `entry_authors__author` cache is reused instead of firing a
                # fresh per-entry query for every feed row.
                for entry_author in sorted(entry.entry_authors.all(), key=lambda ea: ea.position)
            ],
            identifiers=entry_identifier_uris(entry),
            language=entry.language.alpha2 if entry.language_id else None,
            publisher=entry.publisher or None,
            issued=str(entry.published_at) if entry.published_at else None,
            # `Entry.summary` is nullable; a `None` here 500s the whole feed.
            summary=Summary(type="text", value=entry.summary or ""),
        )

        if not complete:
            acquisition_entry.links.append(
                Link(
                    rel=LinkType.ALTERNATE,
                    href=reverse(
                        "opds:complete-entry", kwargs={"catalog_name": entry.catalog.url_name, "entry_id": entry.id}
                    ),
                    type="application/atom+xml;type=entry;profile=opds-catalog",
                    title=f"Complete catalog entry for {entry.title}",
                )
            )

        for category in entry.categories.all():
            acquisition_entry.categories.append(
                Category(
                    term=category.term,
                    scheme=category.scheme,
                    label=localize(category.label),
                )
            )

        if entry.image:
            acquisition_entry.links.append(
                Link(
                    rel=LinkType.IMAGE,
                    href=reverse("files:cover-download", kwargs={"entry_id": entry.id}),
                    type=entry.image_mime,
                )
            )
        if entry.thumbnail:
            acquisition_entry.links.append(
                Link(
                    rel=LinkType.THUMBNAIL,
                    href=reverse("files:thumbnail-download", kwargs={"entry_id": entry.id}),
                    type=entry.thumbnail_mime or entry.image_mime or "image/jpeg",
                )
            )

        readium_enabled = bool(entry.read_config("readium_enabled"))

        # Non-LCP entries: emit the raw acquisition links as before.
        # LCP-protected entries: skip the raw download link so we don't
        # leak the unencrypted file URL (Thorium / Readium-toolkit readers
        # fetch the .lcpl link instead and pull encrypted content from the
        # LCP-registered URL).
        if not readium_enabled:
            for acquisition in entry.acquisitions.all():
                acquisition_entry.links.append(
                    Link(
                        rel=str(Acquisition.AcquisitionType(acquisition.relation)),
                        href=reverse(
                            "files:acquisition-download",
                            kwargs={"acquisition_id": acquisition.pk},
                        ),
                        type=acquisition.mime,
                        checksum=acquisition.checksum if complete else None,
                    )
                )

        # LCP borrow / direct-license links (shared with OPDS 2.0).
        if readium_enabled and request is not None:
            from apps.opds.services.borrow_link import BorrowLinkResolver

            resolver = BorrowLinkResolver(request, opds_version="1.2")
            for borrow_link in resolver.emit_links(entry, user):
                acquisition_entry.links.append(Link.from_borrow_link(borrow_link))

        return acquisition_entry


class OpdsFeed(BaseXmlModel, tag="feed", nsmap=NSMAP):
    id: str = element()  # TODO: URI element
    links: List[Link] = element(tag="link", default=list())
    title: str = element()
    updated: datetime.datetime = element()
    author: Author = element()
    entries: List[Union[NavigationEntry, AcquisitionEntry]] = element(tag="entry", default=list())


OPENSEARCH_NSMAP = {"": "http://a9.com/-/spec/opensearch/1.1/"}


class OpenSearchLink(BaseXmlModel, tag="Url", nsmap=OPENSEARCH_NSMAP):
    type: Literal["application/atom+xml;profile=opds-catalog"] = attr(
        name="type", default="application/atom+xml;profile=opds-catalog"
    )
    template: str = attr(name="template")

    def __init__(self, base_path: str, template_items: Dict[str, str], **data):
        from urllib.parse import quote

        template = base_path + "?" + "&".join(f"{quote(k, safe='')}={{{v}}}" for k, v in template_items.items())
        super().__init__(template=template, **data)


class OpenSearchQuery(BaseXmlModel, tag="Query", nsmap=OPENSEARCH_NSMAP): ...


class OpenSearchDescription(BaseXmlModel, tag="OpenSearchDescription", nsmap=OPENSEARCH_NSMAP):
    short_name: str = element(tag="ShortName")
    description: str = element(tag="Description")
    url: OpenSearchLink = element()
