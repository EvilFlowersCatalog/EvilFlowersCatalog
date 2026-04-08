from http import HTTPStatus

from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import Entry, Feed
from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class NavigationView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        feeds = self.catalog.feeds.filter(parents__isnull=True)
        feed = FeedBuilder.build_navigation_feed(feeds, self.catalog, request)
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))


class FeedView(Opds2CatalogView):
    def get(self, request, catalog_name: str, feed_name: str):
        try:
            custom_feed = Feed.objects.get(catalog=self.catalog, url_name=feed_name)
        except Feed.DoesNotExist:
            raise ProblemDetailException(_("Feed not found"), status=HTTPStatus.NOT_FOUND)

        if custom_feed.kind == Feed.FeedKind.NAVIGATION:
            children = custom_feed.children.all()
            feed = FeedBuilder.build_navigation_feed(children, self.catalog, request)
        else:
            entries = custom_feed.entries.order_by("-created_at")
            page = int(request.GET.get("page", 1))
            feed = FeedBuilder.build_publication_feed(
                entries, self.catalog, request, page=page, title=custom_feed.title
            )

        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))


class NewView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("-created_at")
        page = int(request.GET.get("page", 1))
        feed = FeedBuilder.build_publication_feed(entries, self.catalog, request, page=page, title="New Publications")
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))


class PopularView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("-popularity")
        page = int(request.GET.get("page", 1))
        feed = FeedBuilder.build_publication_feed(
            entries, self.catalog, request, page=page, title="Popular Publications"
        )
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))
