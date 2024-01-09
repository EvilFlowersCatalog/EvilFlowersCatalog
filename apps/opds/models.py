import datetime
from typing import Literal, Optional, List, Union

from pydantic import EmailStr
from pydantic_xml import BaseXmlModel, attr, element

NSMAP = {
    "": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "opds": "http://opds-spec.org/2010/catalog",
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


class Link(BaseXmlModel, tag="link", nsmap=NSMAP):
    rel: Literal[
        "self",
        "start",
        "up",
        "related",
        "subsection",
        "http://opds-spec.org/sort/popular",
        "http://opds-spec.org/sort/new",
    ] = attr()
    href: str = attr()
    type: str = attr()
    title: Optional[str] = attr(default=None)


class OpdsEntry(BaseXmlModel, nsmap=NSMAP):
    title: str = element()
    id: str = element()
    updated: datetime.datetime = element()
    links: List[Link] = element(tag="link")


class NavigationEntry(OpdsEntry, tag="entry"):
    author: Optional[Author] = element(default=None)
    content: Content = element()


class AcquisitionEntry(OpdsEntry, tag="entry"):
    authors: List[Author] = element(tag="author")
    summary: Summary = element()
    content: Optional[Content] = element(default=None)


class OpdsFeed(BaseXmlModel, tag="feed", nsmap=NSMAP):
    id: str = element()  # TODO: URI element
    links: List[Link] = element(tag="link")
    title: str = element()
    updated: datetime.datetime = element()
    author: Author = element()
    entries: List[Union[NavigationEntry, AcquisitionEntry]] = element(tag="entry")
