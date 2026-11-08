from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.fields.localized import localize, request_language
from apps.opds.schema import (
    ACQUISITION_FEED_TYPE,
    AUTHENTICATION_DOCUMENT_TYPE,
    NAVIGATION_FEED_TYPE,
    Author,
    Content,
    Link,
    LinkType,
    NavigationEntry,
    OpdsFeed,
    feed_type,
)
from apps.opds.views.base import OpdsCatalogView


class RootView(OpdsCatalogView):
    def _entry(self, request, url_name: str, title: str, rel: LinkType, link_type: str, updated, **kwargs):
        href = reverse(url_name, kwargs={"catalog_name": self.catalog.url_name, **kwargs})
        return NavigationEntry(
            id=request.build_absolute_uri(href),
            title=title,
            links=[Link(rel=rel, href=href, type=link_type)],
            content=Content(type="text", value=title),
            updated=updated,
        )

    def get(self, request, catalog_name: str):
        feeds = self.catalog.feeds.filter(parents__isnull=True)
        root = reverse("opds:root", kwargs={"catalog_name": catalog_name})

        links = [
            Link(rel=LinkType.SELF, href=root, type=NAVIGATION_FEED_TYPE),
            Link(rel=LinkType.START, href=root, type=NAVIGATION_FEED_TYPE),
            Link(
                rel=LinkType.SEARCH,
                href=reverse("opds:search-descriptor", kwargs={"catalog_name": catalog_name}),
                type="application/opensearchdescription+xml",
            ),
            # OPDS 1.2 §2.5: the Complete Acquisition Feed, for crawlers.
            Link(
                rel=LinkType.CRAWLABLE,
                href=reverse("opds:complete", kwargs={"catalog_name": catalog_name}),
                type=ACQUISITION_FEED_TYPE,
            ),
            # Authentication for OPDS 1.0: lets a reader discover how to sign
            # in before it hits a 401 (borrowing needs an account).
            Link(
                rel=LinkType.AUTH_DOCUMENT,
                href=request.build_absolute_uri(reverse("opds2:catalog-auth", kwargs={"catalog_name": catalog_name})),
                type=AUTHENTICATION_DOCUMENT_TYPE,
            ),
        ]
        if request.user.is_authenticated:
            links.append(
                Link(
                    rel=LinkType.SHELF,
                    href=reverse("opds:shelf", kwargs={"catalog_name": catalog_name}),
                    type=ACQUISITION_FEED_TYPE,
                )
            )

        result = OpdsFeed(
            id=request.build_absolute_uri(root),
            title=self.catalog.title,
            links=links,
            updated=self.catalog.touched_at,
            author=Author(
                name=self.catalog.creator.full_name,
                uri=f"urn:uuid:{self.catalog.creator.id}",
            ),
            entries=[],
        )

        # Popular, New and the shelf are acquisition feeds of entries.
        result.entries.append(
            self._entry(
                request,
                "opds:popular",
                _("Popular in %s") % (self.catalog.title,),
                LinkType.POPULAR,
                ACQUISITION_FEED_TYPE,
                self.catalog.touched_at,
            )
        )
        result.entries.append(
            self._entry(
                request,
                "opds:new",
                _("New in %s") % (self.catalog.title,),
                LinkType.NEW,
                ACQUISITION_FEED_TYPE,
                self.catalog.touched_at,
            )
        )

        if request.user.is_authenticated:
            result.entries.append(
                self._entry(
                    request,
                    "opds:shelf",
                    _("Shelf of %s inside %s") % (request.user.full_name, self.catalog.title),
                    LinkType.SHELF,
                    ACQUISITION_FEED_TYPE,
                    self.catalog.touched_at,
                )
            )

        for feed in feeds:
            entry = self._entry(
                request,
                "opds:feed",
                localize(feed.title, request_language(request)),
                LinkType.SUBSECTION,
                feed_type(feed.kind),
                feed.updated_at,
                feed_name=feed.url_name,
            )
            entry.content = Content(type="text", value=feed.content or entry.title)
            result.entries.append(entry)

        return HttpResponse(
            result.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type=NAVIGATION_FEED_TYPE,
        )
