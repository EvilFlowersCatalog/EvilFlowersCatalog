from typing import Literal, Optional, List

from pydantic import EmailStr, HttpUrl
from pydantic_xml import BaseXmlModel, attr, element

NSMAP = {
    "": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/terms/",
    "opds": "http://opds-spec.org/2010/catalog",
}


class Author(BaseXmlModel, tag="author", nsmap=NSMAP):
    name: str = element()
    email: Optional[EmailStr] = element()
    uri: Optional[HttpUrl] = element()


class Content(BaseXmlModel, tag="content", nsmap=NSMAP):
    type: str = attr()
    value: str


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
    title: Optional[str] = attr()


class NavigationEntry(BaseXmlModel, tag="entry"):
    title: str = element()
    links: List[Link] = element(tag="link")
    updated: str = element()
    id: str = element()
    content: Content = element()


class Feed(BaseXmlModel, tag="feed", nsmap={"": "http://www.w3.org/2005/Atom"}):
    id: str = element()  # TODO: URI element
    links: List[Link] = element(tag="link")
    title: str = element()
    updated: str = element()
    author: Author = element()
    entries: List[NavigationEntry] = element(tag="entry")
