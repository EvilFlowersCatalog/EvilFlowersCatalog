import datetime
from enum import Enum
from typing import Optional, List, Union

from pydantic import EmailStr
from pydantic_xml import BaseXmlModel, attr, element

NSMAP = {
    "": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "opds": "http://opds-spec.org/2010/catalog",
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
    UP = "up"
    RELATED = "related"
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


class OpdsFeed(BaseXmlModel, tag="feed", nsmap=NSMAP):
    id: str = element()  # TODO: URI element
    links: List[Link] = element(tag="link", default=list())
    title: str = element()
    updated: datetime.datetime = element()
    author: Author = element()
    entries: List[Union[NavigationEntry, AcquisitionEntry]] = element(tag="entry", default=list())
