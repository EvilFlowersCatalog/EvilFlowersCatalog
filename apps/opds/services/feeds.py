import datetime
from abc import ABC, abstractmethod
from typing import List, Optional

from django.conf import settings
from django.db.models import QuerySet
from django.urls import reverse

from apps.core.models import Entry, Acquisition, Feed, User
from apps.opds.schema import (
    OpdsFeed,
    Link,
    AcquisitionEntry,
    LinkType,
    Author,
    Summary,
    Category,
    NavigationEntry,
    Content,
)


class BaseFeed(ABC):
    def __init__(
        self,
        opds_feed_id: str,
        title: str,
        author: User,
        updated_at: datetime.datetime,
        links: List[Link] = None,
        qs: Optional[QuerySet] = None,
        **kwargs,
    ):
        self._id: str = opds_feed_id
        self._title: str = title
        self._author: User = author
        self._updated_at: datetime.datetime = updated_at
        self._entries: List[AcquisitionEntry | NavigationEntry] = []
        self._links: List[Link] = links or []
        self._extras = kwargs

        if qs:
            for entry in qs.all():
                self.add_entry(entry)

    @abstractmethod
    def add_entry(self, entry):
        pass

    def add_link(self, rel: LinkType, href: str, link_type: str, title: Optional[str] = None):
        self._links.append(Link(rel=rel, href=href, type=link_type, title=title))

    def serialize(self) -> OpdsFeed:
        return OpdsFeed(
            id=self._id,
            title=self._title,
            author=Author(name=self._author.full_name),
            updated=self._updated_at,
            links=self._links,
            entries=self._entries,
        )

    def to_xml(self):
        return self.serialize().to_xml(
            pretty_print=settings.DEBUG,
            encoding="UTF-8",
            standalone=True,
            skip_empty=True,
        )


class NavigationFeed(BaseFeed):
    def add_entry(self, feed: Feed):
        self._entries.append(
            NavigationEntry(
                id=f"urn:uuid:{feed.pk}",
                title=feed.title,
                links=[
                    Link(
                        rel=LinkType.SUBSECTION,
                        href=reverse(
                            "opds:feed",
                            kwargs={
                                "catalog_name": feed.catalog.url_name,
                                "feed_name": feed.url_name,
                            },
                        ),
                        type="application/atom+xml;profile=opds;kind=navigation",
                    )
                ],
                content=Content(type="text", value=feed.content),
                updated=feed.updated_at,
            )
        )


class AcquisitionFeed(BaseFeed):
    def add_entry(self, entry: Entry):
        self._entries.append(AcquisitionEntry.from_model(entry, self._extras.get("complete", False)))
