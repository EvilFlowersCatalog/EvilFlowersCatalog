from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.api.filters.entries import EntryFilter
from apps.core.errors import ProblemDetailException
from apps.core.models import Feed, Entry
from apps.opds.models import (
    Link,
    Author,
    OpdsFeed,
    LinkType,
)
from apps.opds.services.feeds import AcquisitionFeed, NavigationFeed
from apps.opds.views.base import OpdsView


class FeedView(OpdsView):
    def get(self, request, catalog_name: str, feed_name: str):
        try:
            feed = Feed.objects.get(catalog=self.catalog, url_name=feed_name)
        except Feed.DoesNotExist:
            raise ProblemDetailException(_("Feed not found"), status=HTTPStatus.NOT_FOUND)

        if feed.kind == Feed.FeedKind.ACQUISITION:
            result = AcquisitionFeed(
                request.build_absolute_uri(
                    reverse(
                        "opds:feed",
                        kwargs={"catalog_name": catalog_name, "feed_name": feed_name},
                    )
                ),
                title=feed.title,
                author=feed.creator,
                updated_at=feed.touched_at,
            )

            result.add_link(
                rel=LinkType.SELF,
                href=reverse(
                    "opds:feed",
                    kwargs={"catalog_name": catalog_name, "feed_name": feed_name},
                ),
                link_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
            )
            result.add_link(
                rel=LinkType.START,
                href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                link_type="application/atom+xml;profile=opds-catalog;kind=navigation",
            )

            for related_feed in feed.parents.all():
                result.add_link(
                    rel=LinkType.RELATED,
                    href=reverse(
                        "opds:feed",
                        kwargs={
                            "catalog_name": catalog_name,
                            "feed_name": related_feed.url_name,
                        },
                    ),
                    link_type="application/atom+xml;profile=opds-catalog;kind=navigation",
                )

            try:
                result.updated = Entry.objects.filter(feeds=feed).latest("updated_at").updated_at
            except Entry.DoesNotExist:
                pass

            for entry in feed.entries.all():
                result.add_entry(entry)

        elif feed.kind == Feed.FeedKind.NAVIGATION:
            result = NavigationFeed(
                request.build_absolute_uri(
                    reverse(
                        "opds:feed",
                        kwargs={"catalog_name": catalog_name, "feed_name": feed_name},
                    )
                ),
                title=feed.title,
                author=feed.creator,
                updated_at=feed.touched_at,
            )

            result.add_link(
                rel=LinkType.SELF,
                href=reverse(
                    "opds:feed",
                    kwargs={"catalog_name": catalog_name, "feed_name": feed_name},
                ),
                link_type="application/atom+xml;profile=opds-catalog;kind=navigation",
            )
            result.add_link(
                rel=LinkType.START,
                href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                link_type="application/atom+xml;profile=opds-catalog;kind=navigation",
            )

            for child in feed.children.all():
                result.add_entry(
                    child,
                    request.build_absolute_uri(
                        reverse(
                            "opds:feed",
                            kwargs={
                                "catalog_name": self.catalog.url_name,
                                "feed_name": child.url_name,
                            },
                        )
                    ),
                )
        else:
            raise ProblemDetailException(_("Invalid feed type"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

        return HttpResponse(
            result.serialize().to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type=f"application/atom+xml;profile=opds-catalog;kind={feed.kind}",
        )


class CompleteFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        result = OpdsFeed(
            id=request.build_absolute_uri(
                reverse(
                    "opds:complete",
                    kwargs={"catalog_name": catalog_name},
                )
            ),
            title=_("Complete %s feed") % (self.catalog.title,),
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse(
                        "opds:complete",
                        kwargs={"catalog_name": catalog_name},
                    ),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                )
            ],
            author=Author(name=self.catalog.creator.full_name),
            updated=self.catalog.touched_at,
            entries=[],
        )

        return HttpResponse(
            result.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class LatestFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("created_at")[
            : settings.EVILFLOWERS_FEEDS_NEW_LIMIT
        ]
        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.filter(pk__in=entries), request=request)

        try:
            updated_at = Entry.objects.filter(id__in=entries).latest("updated_at").updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(
            request,
            "opds/feeds/latest.xml",
            {
                "catalog": self.catalog,
                "updated_at": updated_at,
                "entry_filter": entry_filter,
            },
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class PopularFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("-popularity")[
            : settings.EVILFLOWERS_FEEDS_NEW_LIMIT
        ]
        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.filter(pk__in=entries), request=request)

        try:
            updated_at = Entry.objects.filter(id__in=entries).latest("updated_at").updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(
            request,
            "opds/feeds/popular.xml",
            {
                "catalog": self.catalog,
                "updated_at": updated_at,
                "entry_filter": entry_filter,
            },
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class ShelfFeedView(OpdsView):
    def get(self):
        return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)
