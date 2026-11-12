import uuid
from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.fields.localized import localize, request_language
from apps.core.errors import ProblemDetailException, AuthorizationException
from apps.core.models import Feed, Entry
from apps.opds.schema import (
    Link,
    LinkType,
)
from apps.opds.services.conditional import feed_etag, finalize, not_modified
from apps.opds.services.feeds import AcquisitionFeed, NavigationFeed
from apps.opds.services.paging import FeedPage
from apps.opds.views.base import OpdsCatalogView

ACQUISITION_CONTENT_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
NAVIGATION_CONTENT_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"

# Total order for paging: a tie on `created_at` (bulk imports) must not let
# an entry repeat or vanish between pages.
NEWEST_FIRST = ("-created_at", "-pk")


def _with_acquisition_prefetch(qs):
    """Eager-load everything `AcquisitionEntry.from_model` touches per entry.

    Without this an N-entry feed fans out to ~3N extra queries (authors,
    categories, acquisitions) plus a catalog lookup per row. Matches the
    Python-side `sorted(entry_authors)` in `apps/opds/schema.py`.
    """
    return qs.select_related("catalog", "language").prefetch_related(
        "entry_authors__author",
        "categories",
        "acquisitions__encrypted_content",
    )


def _page_entries(page: FeedPage) -> list:
    return page.load(_with_acquisition_prefetch(Entry.objects.all()))


def _feed_urn(catalog, name: str) -> str:
    """Stable Atom id for a catalog's derived feed (new, popular, shelf)."""
    return f"urn:uuid:{uuid.uuid5(catalog.pk, name)}"


class FeedView(OpdsCatalogView):
    def get(self, request, catalog_name: str, feed_name: str):
        try:
            feed = Feed.objects.get(catalog=self.catalog, url_name=feed_name)
        except Feed.DoesNotExist:
            raise ProblemDetailException(_("Feed not found"), status=HTTPStatus.NOT_FOUND)

        path = reverse("opds:feed", kwargs={"catalog_name": catalog_name, "feed_name": feed_name})

        if feed.kind == Feed.FeedKind.ACQUISITION:
            page = FeedPage.resolve(
                request,
                feed.entries.order_by(*NEWEST_FIRST),
                max(feed.per_page or settings.EVILFLOWERS_OPDS_PAGE_SIZE, 1),
            )
            parents = list(feed.parents.order_by("pk"))
            etag = feed_etag(
                request,
                head=(
                    "feed",
                    feed.pk,
                    feed.updated_at,
                    feed.touched_at,
                    page.number,
                    page.num_pages,
                    *(f"{parent.pk}@{parent.updated_at}" for parent in parents),
                ),
                entries=page.rows,
            )
            if (response := not_modified(request, etag)) is not None:
                return response

            result = AcquisitionFeed(
                request.build_absolute_uri(path),
                title=localize(feed.title, request_language(request)),
                author=feed.creator,
                updated_at=feed.touched_at,
                entries=_page_entries(page),
                links=page.links(request, path),
                request=request,
                user=request.user,
            )

            result.add_link(rel=LinkType.SELF, href=path, link_type=ACQUISITION_CONTENT_TYPE)
            result.add_link(
                rel=LinkType.START,
                href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                link_type=NAVIGATION_CONTENT_TYPE,
            )

            for related_feed in parents:
                result.add_link(
                    rel=LinkType.RELATED,
                    href=reverse(
                        "opds:feed",
                        kwargs={
                            "catalog_name": catalog_name,
                            "feed_name": related_feed.url_name,
                        },
                    ),
                    link_type=NAVIGATION_CONTENT_TYPE,
                )

        elif feed.kind == Feed.FeedKind.NAVIGATION:
            children = list(feed.children.select_related("catalog").order_by("pk"))
            etag = feed_etag(
                request,
                head=(
                    "feed",
                    feed.pk,
                    feed.updated_at,
                    feed.touched_at,
                    *(f"{child.pk}@{child.updated_at}" for child in children),
                ),
            )
            if (response := not_modified(request, etag)) is not None:
                return response

            result = NavigationFeed(
                request.build_absolute_uri(path),
                title=localize(feed.title, request_language(request)),
                author=feed.creator,
                updated_at=feed.touched_at,
                entries=children,
                language=request_language(request),
            )

            result.add_link(rel=LinkType.SELF, href=path, link_type=NAVIGATION_CONTENT_TYPE)
            result.add_link(
                rel=LinkType.START,
                href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                link_type=NAVIGATION_CONTENT_TYPE,
            )
        else:
            raise ProblemDetailException(_("Invalid feed type"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

        return finalize(
            request,
            HttpResponse(result.to_xml(), content_type=f"application/atom+xml;profile=opds-catalog;kind={feed.kind}"),
            etag,
        )


class CompleteFeedView(OpdsCatalogView):
    """OPDS 1.2 §2.5 Complete Acquisition Feed.

    Served whole with `fh:complete`, ordered by `atom:updated` descending, as
    the spec requires. Only a catalog above
    `EVILFLOWERS_OPDS_COMPLETE_FEED_MAX_ENTRIES` is paged ("should not be
    paginated unless they are extremely large"), and then without
    `fh:complete`.
    """

    def get(self, request, catalog_name: str):
        page = FeedPage.resolve(
            request,
            self.catalog.entries.order_by("-updated_at", "-pk"),
            settings.EVILFLOWERS_OPDS_COMPLETE_FEED_MAX_ENTRIES,
        )
        etag = feed_etag(
            request,
            head=(
                "complete",
                self.catalog.pk,
                self.catalog.updated_at,
                self.catalog.touched_at,
                page.number,
                page.num_pages,
            ),
            entries=page.rows,
        )
        if (response := not_modified(request, etag)) is not None:
            return response

        path = reverse("opds:complete", kwargs={"catalog_name": catalog_name})
        result = AcquisitionFeed(
            f"urn:uuid:{self.catalog.pk}",
            title=_("Complete %s feed") % (self.catalog.title,),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            entries=_page_entries(page),
            links=[
                Link(rel=LinkType.SELF, href=path, type=ACQUISITION_CONTENT_TYPE),
                *page.links(request, path),
            ],
            fh_complete=not page.is_paged,
            complete=True,
            request=request,
            user=request.user,
        )

        return finalize(request, HttpResponse(result.to_xml(), content_type=ACQUISITION_CONTENT_TYPE), etag)


class _TopEntriesFeedView(OpdsCatalogView):
    """A short, unpaged feed of the catalog's first N entries in some order."""

    feed_name: str
    url_name: str
    ordering: tuple

    def title(self) -> str:
        raise NotImplementedError

    def get(self, request, catalog_name: str):
        page = FeedPage.whole(
            Entry.objects.filter(catalog=self.catalog).order_by(*self.ordering),
            limit=settings.EVILFLOWERS_FEEDS_NEW_LIMIT,
        )
        etag = feed_etag(
            request,
            head=(self.feed_name, self.catalog.pk, self.catalog.updated_at, self.catalog.touched_at),
            entries=page.rows,
        )
        if (response := not_modified(request, etag)) is not None:
            return response

        result = AcquisitionFeed(
            _feed_urn(self.catalog, self.feed_name),
            title=self.title(),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            entries=_page_entries(page),
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse(self.url_name, kwargs={"catalog_name": catalog_name}),
                    type=ACQUISITION_CONTENT_TYPE,
                )
            ],
            request=request,
            user=request.user,
        )

        return finalize(request, HttpResponse(result.to_xml(), content_type=ACQUISITION_CONTENT_TYPE), etag)


class LatestFeedView(_TopEntriesFeedView):
    feed_name = "new"
    url_name = "opds:new"
    ordering = NEWEST_FIRST

    def title(self) -> str:
        return _("Latest in %s") % (self.catalog.title,)


class PopularFeedView(_TopEntriesFeedView):
    feed_name = "popular"
    url_name = "opds:popular"
    ordering = ("-popularity", "-pk")

    def title(self) -> str:
        return _("Popular in %s") % (self.catalog.title,)


class ShelfFeedView(OpdsCatalogView):
    def get(self, request, catalog_name: str):
        if request.user.is_anonymous:
            raise AuthorizationException(request)

        page = FeedPage.resolve(
            request,
            Entry.objects.filter(catalog=self.catalog, shelf_records__user=request.user).order_by(*NEWEST_FIRST),
            settings.EVILFLOWERS_OPDS_PAGE_SIZE,
        )
        etag = feed_etag(
            request,
            head=(
                "shelf",
                self.catalog.pk,
                self.catalog.updated_at,
                self.catalog.touched_at,
                page.number,
                page.num_pages,
            ),
            entries=page.rows,
        )
        if (response := not_modified(request, etag)) is not None:
            return response

        path = reverse("opds:shelf", kwargs={"catalog_name": catalog_name})
        result = AcquisitionFeed(
            _feed_urn(self.catalog, f"shelf:{request.user.pk}"),
            title=_("Shelf of %s inside %s")
            % (
                request.user.full_name,
                self.catalog.title,
            ),
            author=request.user,
            updated_at=max((touched for _, touched in page.rows if touched), default=self.catalog.touched_at),
            entries=_page_entries(page),
            links=[
                Link(rel=LinkType.SELF, href=path, type=ACQUISITION_CONTENT_TYPE),
                *page.links(request, path),
            ],
            request=request,
            user=request.user,
        )

        return finalize(request, HttpResponse(result.to_xml(), content_type=ACQUISITION_CONTENT_TYPE), etag)
