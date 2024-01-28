from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse

from apps.opds.models import OpdsFeed, Link, Author, NavigationEntry, Content
from apps.opds.views.base import OpdsView


class RootView(OpdsView):
    def get(self, request, catalog_name: str):
        feeds = self.catalog.feeds.filter(parents__content__isnull=True)

        result = OpdsFeed(
            id=request.build_absolute_uri(reverse("opds:root", kwargs={"catalog_name": catalog_name})),
            title=self.catalog.title,
            links=[
                Link(
                    rel="self",
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
                Link(
                    rel="start",
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
            ],
            updated=self.catalog.updated_at,
            author=Author(
                name=self.catalog.creator.full_name,
                uri=f"urn:uuid:{self.catalog.creator.id}",
            ),
            entries=[],
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
                            rel="subsection",
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
