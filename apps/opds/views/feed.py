import uuid
from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException, AuthorizationException
from apps.core.models import Feed, Entry
from apps.opds.models import (
    Link,
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
                qs=feed.entries.all(),
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
                qs=feed.children.all(),
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
        result = AcquisitionFeed(
            f"urn:uuid:{self.catalog.pk}",
            title=_("Complete %s feed") % (self.catalog.title,),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            qs=self.catalog.entries.order_by("-created_at"),
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
            complete=True,
        )

        return HttpResponse(
            result.to_xml(),
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class LatestFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("created_at")[
            : settings.EVILFLOWERS_FEEDS_NEW_LIMIT
        ]

        result = AcquisitionFeed(
            f"urn:uuid:{uuid.uuid4()}",
            title=_("Latest in %s") % (self.catalog.title,),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            qs=entries,
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse(
                        "opds:new",
                        kwargs={"catalog_name": catalog_name},
                    ),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                )
            ],
        )

        return HttpResponse(
            result.to_xml(),
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class PopularFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("-popularity")[
            : settings.EVILFLOWERS_FEEDS_NEW_LIMIT
        ]

        result = AcquisitionFeed(
            f"urn:uuid:{uuid.uuid4()}",
            title=_("Popular in %s") % (self.catalog.title,),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            qs=entries,
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse(
                        "opds:popular",
                        kwargs={"catalog_name": catalog_name},
                    ),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                )
            ],
        )

        return HttpResponse(
            result.to_xml(),
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )


class ShelfFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        if request.user.is_anonymous:
            raise AuthorizationException(request)

        try:
            updated_at = Entry.objects.filter(shelf_records__user=request.user).latest("touched_at").touched_at
        except Entry.DoesNotExist:
            updated_at = timezone.now()

        result = AcquisitionFeed(
            f"urn:uuid:{uuid.uuid4()}",
            title=_("Shelf of %s inside %s")
            % (
                request.user.full_name,
                self.catalog.title,
            ),
            author=request.user,
            updated_at=updated_at,
            qs=Entry.objects.filter(shelf_records__user=request.user),
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse(
                        "opds:shelf",
                        kwargs={"catalog_name": catalog_name},
                    ),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                )
            ],
        )

        return HttpResponse(
            result.to_xml(),
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )
