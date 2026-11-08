from django.conf import settings
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from apps.core.fields.localized import localize, request_language
from apps.core.models import Catalog, Entry, Feed
from apps.opds.services.borrow_link import LICENSED_STATES
from apps.readium.models import Reservation
from apps.api.services.facets import facet_href, opds_facets
from apps.opds2.schema.opds import NavigationEntry, OpdsFeed
from apps.opds2.schema.opds.facet import Facet
from apps.opds2.schema.rwpm import Link, Metadata, Publication
from apps.opds2.schema.rwpm.link import Properties
from .manifest_builder import ManifestBuilder

OPDS_JSON = "application/opds+json"


class FeedBuilder:
    """Builds OPDS 2.0 JSON feeds from Django models."""

    @staticmethod
    def _sorted_by_title(feeds: QuerySet, language: str) -> list:
        return sorted(feeds, key=lambda feed: (localize(feed.title, language) or "").casefold())

    @classmethod
    def _base_url(cls, request: HttpRequest) -> str:
        return f"{request.scheme}://{request.get_host()}"

    @classmethod
    def _opds2_url(cls, request: HttpRequest, name: str, **kwargs) -> str:
        return f"{cls._base_url(request)}{reverse(f'opds2:{name}', kwargs=kwargs)}"

    @classmethod
    def build_catalog_feed(cls, catalog: Catalog, request: HttpRequest) -> OpdsFeed:
        base = cls._base_url(request)
        cn = catalog.url_name

        navigation = [
            NavigationEntry(
                href=cls._opds2_url(request, "publications", catalog_name=cn),
                title="All Publications",
                type=OPDS_JSON,
                rel="subsection",
            ),
            NavigationEntry(
                href=cls._opds2_url(request, "new", catalog_name=cn),
                title="New Publications",
                type=OPDS_JSON,
                rel="http://opds-spec.org/sort/new",
            ),
            NavigationEntry(
                href=cls._opds2_url(request, "popular", catalog_name=cn),
                title="Popular Publications",
                type=OPDS_JSON,
                rel="http://opds-spec.org/sort/popular",
            ),
        ]

        if request.user.is_authenticated:
            navigation.append(
                NavigationEntry(
                    href=cls._opds2_url(request, "shelf", catalog_name=cn),
                    title="My Shelf",
                    type=OPDS_JSON,
                    rel="http://opds-spec.org/shelf",
                )
            )

        # Add custom feeds
        language = request_language(request)
        for feed in cls._sorted_by_title(catalog.feeds.filter(parents__isnull=True), language):
            navigation.append(
                NavigationEntry(
                    href=cls._opds2_url(request, "feed", catalog_name=cn, feed_name=feed.url_name),
                    title=localize(feed.title, language),
                    type=OPDS_JSON,
                    rel="subsection",
                )
            )

        links = [
            Link(
                href=cls._opds2_url(request, "catalog", catalog_name=cn),
                type=OPDS_JSON,
                rel="self",
            ),
            Link(
                href=cls._opds2_url(request, "catalog", catalog_name=cn),
                type=OPDS_JSON,
                rel="start",
            ),
            Link(
                href=cls._opds2_url(request, "search", catalog_name=cn) + "{?query}",
                type=OPDS_JSON,
                rel="search",
                templated=True,
            ),
            # Authentication for OPDS 1.0 §2.2: advertised whether or not the
            # request is signed in, so a reader can discover sign-in up front.
            # The catalog's own document: it links this catalog's shelf, which
            # Thorium's catalog-card icon opens.
            Link(
                href=f"{base}{reverse('opds2:catalog-auth', kwargs={'catalog_name': cn})}",
                type="application/opds-authentication+json",
                rel="http://opds-spec.org/auth/document",
            ),
        ]
        if request.user.is_authenticated:
            links.append(
                Link(
                    href=cls._opds2_url(request, "shelf", catalog_name=cn),
                    type=OPDS_JSON,
                    rel="http://opds-spec.org/shelf",
                )
            )

        return OpdsFeed(
            metadata=Metadata(title=catalog.title),
            links=links,
            navigation=navigation,
        )

    @classmethod
    def build_publication_feed(
        cls,
        entries: QuerySet,
        catalog: Catalog,
        request: HttpRequest,
        page: int = 1,
        per_page: int | None = None,
        title: str = "Publications",
        self_url: str | None = None,
        facets: dict | None = None,
    ) -> OpdsFeed:
        """`facets`: counts from `EntrySearchService.catalog_facets`, rendered as OPDS 2.0 facets."""
        if per_page is None:
            per_page = getattr(settings, "EVILFLOWERS_OPDS2_PAGE_SIZE", 50)
        per_page = min(per_page, getattr(settings, "EVILFLOWERS_OPDS2_MAX_PAGE_SIZE", 100))

        total = entries.count()
        offset = (page - 1) * per_page
        # A total order, or rows repeat or vanish between pages.
        ordering = [field for field in entries.query.order_by if field not in ("pk", "-pk")] or ["-created_at"]
        page_entries = entries.order_by(*ordering, "-pk")[offset : offset + per_page]

        publications = []
        for entry in page_entries.select_related("catalog", "language").prefetch_related(
            "entry_authors__author", "categories", "acquisitions__encrypted_content"
        ):
            publications.append(ManifestBuilder.build_publication(entry, request, user=request.user))

        if self_url is None:
            self_url = request.build_absolute_uri()

        links = [
            Link(href=self_url, type=OPDS_JSON, rel="self"),
            Link(href=cls._opds2_url(request, "catalog", catalog_name=catalog.url_name), type=OPDS_JSON, rel="start"),
        ]

        total_pages = max((total + per_page - 1) // per_page, 1) if per_page else 1
        if total_pages > 1:
            links.append(Link(href=cls._paginated_url(request, 1), type=OPDS_JSON, rel="first"))
        if page > 1:
            links.append(Link(href=cls._paginated_url(request, page - 1), type=OPDS_JSON, rel="previous"))
        if page < total_pages:
            links.append(Link(href=cls._paginated_url(request, page + 1), type=OPDS_JSON, rel="next"))
        if total_pages > 1:
            links.append(Link(href=cls._paginated_url(request, total_pages), type=OPDS_JSON, rel="last"))

        metadata = Metadata(title=title, number_of_items=total, items_per_page=per_page, current_page=page)

        return OpdsFeed(
            metadata=metadata,
            links=links,
            # A feed needs at least one collection, even when it is empty.
            publications=publications,
            facets=cls._facets(request, facets) if facets else None,
        )

    @classmethod
    def _facets(cls, request: HttpRequest, facets: dict) -> list[Facet] | None:
        """OPDS 2.0 §2.3 facets: one group per dimension; the active value links with `rel: self`."""
        groups: dict[str, list[Link]] = {}
        base = request.build_absolute_uri(request.path)
        for facet in opds_facets(request, facets, request_language(request)):
            groups.setdefault(facet.group, []).append(
                Link(
                    href=facet_href(request, facet, base=base),
                    type=OPDS_JSON,
                    title=facet.title,
                    rel="self" if facet.active else None,
                    properties=Properties(number_of_items=facet.count),
                )
            )
        return [Facet(metadata=Metadata(title=group), links=links) for group, links in groups.items()] or None

    @classmethod
    def build_navigation_feed(cls, feeds: QuerySet, catalog: Catalog, request: HttpRequest) -> OpdsFeed:
        base = cls._base_url(request)
        cn = catalog.url_name

        navigation = []
        language = request_language(request)
        for feed in cls._sorted_by_title(feeds, language):
            navigation.append(
                NavigationEntry(
                    href=cls._opds2_url(request, "feed", catalog_name=cn, feed_name=feed.url_name),
                    title=localize(feed.title, language),
                    type=OPDS_JSON,
                    rel="subsection",
                )
            )

        return OpdsFeed(
            metadata=Metadata(title=f"{catalog.title} - Navigation"),
            links=[
                Link(href=request.build_absolute_uri(), type=OPDS_JSON, rel="self"),
            ],
            navigation=navigation,
        )

    @classmethod
    def build_shelf_feed(cls, user, catalog: Catalog, request: HttpRequest) -> OpdsFeed:
        """The user's loans and holds in `catalog` (the OPDS "bookshelf").

        A loan carries the `.lcpl` acquisition link with the loan period; a
        hold carries the borrow link with `reserved` (queue position) or
        `ready` (claim deadline) availability.
        """
        now = timezone.now()
        entries = (
            Entry.objects.filter(catalog=catalog)
            .filter(
                Q(licenses__user=user, licenses__state__in=LICENSED_STATES, licenses__expires_at__gt=now)
                | Q(reservations__user=user, reservations__status__in=Reservation.NON_TERMINAL_STATUSES)
            )
            .distinct()
            .order_by("title", "pk")
            .select_related("catalog", "language")
            .prefetch_related("entry_authors__author", "categories", "acquisitions__encrypted_content")
        )

        publications = [ManifestBuilder.build_publication(entry, request, user=user) for entry in entries]

        return OpdsFeed(
            metadata=Metadata(title="My Shelf", number_of_items=len(publications)),
            links=[
                Link(href=request.build_absolute_uri(), type=OPDS_JSON, rel="self"),
            ],
            # A feed needs at least one collection, even when it is empty.
            publications=publications,
        )

    @classmethod
    def _paginated_url(cls, request: HttpRequest, page: int) -> str:
        params = request.GET.copy()
        params["page"] = str(page)
        return f"{request.build_absolute_uri(request.path)}?{params.urlencode()}"
