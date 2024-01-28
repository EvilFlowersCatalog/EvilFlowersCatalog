from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.api.filters.entries import EntryFilter
from apps.core.errors import ProblemDetailException
from apps.core.models import Feed, Entry, Acquisition
from apps.opds.models import (
    Link,
    Author,
    AcquisitionEntry,
    NavigationEntry,
    Content,
    OpdsFeed,
    Summary,
    Category,
)
from apps.opds.views.base import OpdsView


class FeedView(OpdsView):
    def get(self, request, catalog_name: str, feed_name: str):
        try:
            feed = Feed.objects.get(catalog=self.catalog, url_name=feed_name)
        except Feed.DoesNotExist:
            raise ProblemDetailException(
                _("Feed not found"), status=HTTPStatus.NOT_FOUND
            )

        result = OpdsFeed(
            id=request.build_absolute_uri(
                reverse(
                    "opds:feed",
                    kwargs={"catalog_name": catalog_name, "feed_name": feed_name},
                )
            ),
            title=feed.title,
            links=[
                Link(
                    rel="self",
                    href=reverse(
                        "opds:feed",
                        kwargs={"catalog_name": catalog_name, "feed_name": feed_name},
                    ),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
                Link(
                    rel="start",
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
            ],
            author=Author(name=feed.creator.full_name),
            updated=feed.updated_at,
            entries=[],
        )

        if feed.kind == Feed.FeedKind.ACQUISITION:
            for related_feed in feed.parents.all():
                result.links.append(
                    Link(
                        rel="related",
                        href=reverse(
                            "opds:feed",
                            kwargs={
                                "catalog_name": catalog_name,
                                "feed_name": related_feed.url_name,
                            },
                        ),
                        type="application/atom+xml;profile=opds-catalog;kind=navigation",
                    )
                )

            try:
                result.updated = (
                    Entry.objects.filter(feeds=feed).latest("updated_at").updated_at
                )
            except Entry.DoesNotExist:
                pass

            for entry in Entry.objects.filter(feeds=feed):
                item = AcquisitionEntry(
                    title=entry.title,
                    id=f"urn:uuid:{entry.id}",
                    updated=entry.updated_at,
                    authors=[Author(name=entry.author.full_name)],
                    links=[],
                    categories=[],
                    summary=Summary(type="text", value=entry.summary),
                )

                for contributor in entry.contributors.all():
                    item.authors.append(Author(name=contributor.full_name))

                for category in entry.categories.all():
                    item.categories.append(
                        Category(
                            term=category.term,
                            scheme=category.scheme,
                            label=category.label,
                        )
                    )

                if entry.image:
                    item.links.append(
                        Link(
                            rel="http://opds-spec.org/image",
                            href=reverse(
                                "files:cover-download", kwargs={"entry_id": entry.id}
                            ),
                            type=entry.image_mime,
                        )
                    )

                for acquisition in entry.acquisitions.all():
                    item.links.append(
                        Link(
                            rel=str(Acquisition.AcquisitionType(acquisition.relation)),
                            href=reverse(
                                "files:acquisition-download",
                                kwargs={"acquisition_id": acquisition.pk},
                            ),
                            type=acquisition.mime,
                        )
                    )

                result.entries.append(item)
        elif feed.kind == Feed.FeedKind.NAVIGATION:
            for child in feed.children.all():
                result.entries.append(
                    NavigationEntry(
                        id=request.build_absolute_uri(
                            reverse(
                                "opds:feed",
                                kwargs={
                                    "catalog_name": self.catalog.url_name,
                                    "feed_name": child.url_name,
                                },
                            )
                        ),
                        title=child.title,
                        links=[
                            Link(
                                rel="subsection",
                                href=reverse(
                                    "opds:feed",
                                    kwargs={
                                        "catalog_name": self.catalog.url_name,
                                        "feed_name": child.url_name,
                                    },
                                ),
                                type="application/atom+xml;profile=opds;kind=navigation",
                            )
                        ],
                        content=Content(type="text", value=child.content),
                        updated=child.updated_at,
                    )
                )

        return HttpResponse(
            result.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type=f"application/atom+xml;profile=opds-catalog;kind={feed.kind}",
        )


class CompleteFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entry_filter = EntryFilter(
            request.GET, queryset=Entry.objects.all(), request=request
        )

        try:
            updated_at = entry_filter.qs.latest("updated_at").updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(
            request,
            "opds/feeds/complete.xml",
            {
                "catalog": self.catalog,
                "updated_at": updated_at,
                "entry_filter": entry_filter,
            },
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class LatestFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("created_at")[
            : settings.EVILFLOWERS_FEEDS_NEW_LIMIT
        ]
        entry_filter = EntryFilter(
            request.GET, queryset=Entry.objects.filter(pk__in=entries), request=request
        )

        try:
            updated_at = (
                Entry.objects.filter(id__in=entries).latest("updated_at").updated_at
            )
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
        entry_filter = EntryFilter(
            request.GET, queryset=Entry.objects.filter(pk__in=entries), request=request
        )

        try:
            updated_at = (
                Entry.objects.filter(id__in=entries).latest("updated_at").updated_at
            )
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
