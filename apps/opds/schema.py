import datetime
from enum import Enum
from typing import Optional, List, Union, Literal, Dict

from django.urls import reverse
from pydantic import EmailStr, Field
from pydantic_xml import BaseXmlModel, attr, element

from apps.core.models import Entry, Acquisition

NSMAP = {
    "": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "opds": "http://opds-spec.org/2010/catalog",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "ef": "http://elvira.dital/schema/evilflowers-opds.xsd",
}


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
    term: str
    scheme: Optional[str] = element(default=None)
    label: Optional[str] = element(default=None)


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
    OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"
    ACQUISITION = "http://opds-spec.org/acquisition"


class Link(BaseXmlModel, tag="link", nsmap=NSMAP):
    rel: LinkType = attr()
    href: str = attr()
    type: str = attr()
    title: Optional[str] = attr(default=None)
    checksum: Optional[str] = attr(ns="ef", default=None)


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
    summary: Summary = element()
    categories: Optional[List[Category]] = element(tag="category", default=list())
    content: Optional[Content] = element(default=None)

    @classmethod
    def from_model(cls, entry: Entry, complete: bool = False) -> "AcquisitionEntry":
        acquisition_entry = AcquisitionEntry(
            title=entry.title,
            id=f"urn:uuid:{entry.id}",
            updated=entry.updated_at,
            authors=[
                Author(name=entry_author.author.full_name) for entry_author in entry.entry_authors.order_by("position")
            ],
            summary=Summary(type="text", value=entry.summary),
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
                    label=category.label,
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

        for acquisition in entry.acquisitions.all():
            acquisition_entry.links.append(
                Link(
                    rel=str(Acquisition.AcquisitionType(acquisition.relation)),  # FIXME: WTF?
                    href=reverse(
                        "files:acquisition-download",
                        kwargs={"acquisition_id": acquisition.pk},
                    ),
                    type=acquisition.mime,
                    checksum=acquisition.checksum if complete else None,
                )
            )

        return acquisition_entry


class OpdsFeed(BaseXmlModel, tag="feed", nsmap=NSMAP):
    id: str = element()  # TODO: URI element
    links: List[Link] = element(tag="link", default=list())
    title: str = element()
    updated: datetime.datetime = element()
    author: Author = element()
    entries: List[Union[NavigationEntry, AcquisitionEntry]] = element(tag="entry", default=list())


class OpenSearchLink(BaseXmlModel, tag="Url", nsmap=NSMAP):
    type: Literal["application/atom+xml;profile=opds-catalog"] = attr(
        name="type", default="application/atom+xml;profile=opds-catalog"
    )
    template: str = attr(name="template")

    def __init__(self, base_path: str, template_items: Dict[str, str], **data):
        template = base_path + "?" + "&".join(f"{k}={{{v}}}" for k, v in template_items.items())
        super().__init__(template=template, **data)


class OpenSearchQuery(BaseXmlModel, tag="Query", nsmap=NSMAP): ...
