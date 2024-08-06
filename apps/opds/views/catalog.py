from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.opds.schema import OpdsFeed, Link, Author, NavigationEntry, Content, LinkType
from apps.opds.views.base import OpdsView


class RootView(OpdsView):
    def get(self, request, catalog_name: str):
        feeds = self.catalog.feeds.filter(parents__content__isnull=True)

        result = OpdsFeed(
            id=request.build_absolute_uri(reverse("opds:root", kwargs={"catalog_name": catalog_name})),
            title=self.catalog.title,
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
                Link(
                    rel=LinkType.START,
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
            ],
            updated=self.catalog.touched_at,
            author=Author(
                name=self.catalog.creator.full_name,
                uri=f"urn:uuid:{self.catalog.creator.id}",
            ),
            entries=[],
        )

        # Popular
        result.entries.append(
            NavigationEntry(
                id=request.build_absolute_uri(
                    reverse(
                        "opds:popular",
                        kwargs={
                            "catalog_name": self.catalog.url_name,
                        },
                    )
                ),
                title=_("Popular in %s") % (self.catalog.title,),
                links=[
                    Link(
                        rel=LinkType.POPULAR,
                        href=reverse(
                            "opds:popular",
                            kwargs={
                                "catalog_name": self.catalog.url_name,
                            },
                        ),
                        type="application/atom+xml;profile=opds;kind=navigation",
                    )
                ],
                content=Content(type="text", value=_("Popular in %s") % (self.catalog.title,)),
                updated=self.catalog.touched_at,
            )
        )

        # Latest
        result.entries.append(
            NavigationEntry(
                id=request.build_absolute_uri(
                    reverse(
                        "opds:new",
                        kwargs={
                            "catalog_name": self.catalog.url_name,
                        },
                    )
                ),
                title=_("New in %s") % (self.catalog.title,),
                links=[
                    Link(
                        rel=LinkType.NEW,
                        href=reverse(
                            "opds:new",
                            kwargs={
                                "catalog_name": self.catalog.url_name,
                            },
                        ),
                        type="application/atom+xml;profile=opds;kind=navigation",
                    )
                ],
                content=Content(type="text", value=_("New in %s") % (self.catalog.title,)),
                updated=self.catalog.touched_at,
            )
        )

        if request.user.is_authenticated:
            result.entries.append(
                NavigationEntry(
                    id=request.build_absolute_uri(
                        reverse(
                            "opds:shelf",
                            kwargs={
                                "catalog_name": self.catalog.url_name,
                            },
                        )
                    ),
                    title=_("Shelf of %s inside %s")
                    % (
                        request.user.full_name,
                        self.catalog.title,
                    ),
                    links=[
                        Link(
                            rel=LinkType.SHELF,
                            href=reverse(
                                "opds:shelf",
                                kwargs={
                                    "catalog_name": self.catalog.url_name,
                                },
                            ),
                            type="application/atom+xml;profile=opds;kind=navigation",
                        )
                    ],
                    content=Content(
                        type="text",
                        value=_("Shelf of %s inside %s")
                        % (
                            request.user.full_name,
                            self.catalog.title,
                        ),
                    ),
                    updated=self.catalog.touched_at,
                )
            )

        for feed in feeds:
            result.entries.append(
                NavigationEntry(
                    id=request.build_absolute_uri(
                        reverse(
                            "opds:feed",
                            kwargs={
                                "catalog_name": self.catalog.url_name,
                                "feed_name": feed.url_name,
                            },
                        )
                    ),
                    title=feed.title,
                    links=[
                        Link(
                            rel=LinkType.SUBSECTION,
                            href=reverse(
                                "opds:feed",
                                kwargs={
                                    "catalog_name": self.catalog.url_name,
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

        return HttpResponse(
            result.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type="application/atom+xml;profile=opds-catalog;kind=navigation",
        )
