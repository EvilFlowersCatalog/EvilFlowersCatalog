from django.conf import settings
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse

from apps.core.models import Catalog, Entry, Feed
from apps.readium.models import License
from apps.opds2.schema.opds import NavigationEntry, OpdsFeed
from apps.opds2.schema.rwpm import Link, Metadata, Publication
from .manifest_builder import ManifestBuilder

OPDS_JSON = "application/opds+json"


class FeedBuilder:
    """Builds OPDS 2.0 JSON feeds from Django models."""

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
                rel="http://opds-spec.org/sort/new",
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
        for feed in catalog.feeds.filter(parents__isnull=True).order_by("title"):
            navigation.append(
                NavigationEntry(
                    href=cls._opds2_url(request, "feed", catalog_name=cn, feed_name=feed.url_name),
                    title=feed.title,
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
                href=cls._opds2_url(request, "search", catalog_name=cn) + "{?query}",
                type=OPDS_JSON,
                rel="search",
                templated=True,
            ),
        ]

        if not request.user.is_authenticated:
            links.append(
                Link(
                    href=f"{base}{reverse('opds2:auth')}",
                    type="application/opds-authentication+json",
                    rel="http://opds-spec.org/auth/document",
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
    ) -> OpdsFeed:
        if per_page is None:
            per_page = getattr(settings, "EVILFLOWERS_OPDS2_PAGE_SIZE", 50)
        per_page = min(per_page, getattr(settings, "EVILFLOWERS_OPDS2_MAX_PAGE_SIZE", 100))

        base = cls._base_url(request)
        total = entries.count()
        offset = (page - 1) * per_page
        page_entries = entries[offset : offset + per_page]

        publications = []
        for entry in page_entries.select_related("catalog", "language").prefetch_related(
            "entry_authors__author", "categories", "acquisitions"
        ):
            publications.append(ManifestBuilder.build_publication(entry, base_url=base))

        if self_url is None:
            self_url = request.build_absolute_uri()

        links = [Link(href=self_url, type=OPDS_JSON, rel="self")]

        total_pages = (total + per_page - 1) // per_page if per_page else 1
        if page < total_pages:
            next_url = cls._paginated_url(request, page + 1)
            links.append(Link(href=next_url, type=OPDS_JSON, rel="next"))
        if page > 1:
            prev_url = cls._paginated_url(request, page - 1)
            links.append(Link(href=prev_url, type=OPDS_JSON, rel="previous"))

        metadata = Metadata(title=title)

        return OpdsFeed(
            metadata=metadata,
            links=links,
            publications=publications if publications else None,
        )

    @classmethod
    def build_navigation_feed(cls, feeds: QuerySet, catalog: Catalog, request: HttpRequest) -> OpdsFeed:
        base = cls._base_url(request)
        cn = catalog.url_name

        navigation = []
        for feed in feeds.order_by("title"):
            navigation.append(
                NavigationEntry(
                    href=cls._opds2_url(request, "feed", catalog_name=cn, feed_name=feed.url_name),
                    title=feed.title,
                    type=OPDS_JSON,
                    rel="subsection",
                )
            )

        return OpdsFeed(
            metadata=Metadata(title=f"{catalog.title} - Navigation"),
            links=[
                Link(href=request.build_absolute_uri(), type=OPDS_JSON, rel="self"),
            ],
            navigation=navigation if navigation else None,
        )

    @classmethod
    def build_shelf_feed(cls, user, catalog: Catalog, request: HttpRequest) -> OpdsFeed:
        base = cls._base_url(request)

        active_licenses = (
            License.objects.filter(
                user=user,
                entry__catalog=catalog,
                state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            )
            .select_related("entry__catalog", "entry__language")
            .prefetch_related("entry__entry_authors__author", "entry__categories", "entry__acquisitions")
        )

        publications = []
        for license_obj in active_licenses:
            entry = license_obj.entry
            pub = ManifestBuilder.build_publication(entry, base_url=base, include_availability=False)

            # Add direct license download link
            license_link = Link(
                href=f"{base}{reverse('readium:license-gateway', kwargs={'license_id': license_obj.pk})}",
                type="application/vnd.readium.lcp.license.v1.0+json",
                rel=ManifestBuilder.OPDS_REL_ACQUISITION,
            )
            if pub.links:
                pub.links.append(license_link)
            else:
                pub.links = [license_link]

            publications.append(pub)

        return OpdsFeed(
            metadata=Metadata(title="My Shelf"),
            links=[
                Link(href=request.build_absolute_uri(), type=OPDS_JSON, rel="self"),
            ],
            publications=publications if publications else None,
        )

    @classmethod
    def _paginated_url(cls, request: HttpRequest, page: int) -> str:
        params = request.GET.copy()
        params["page"] = str(page)
        return f"{request.build_absolute_uri(request.path)}?{params.urlencode()}"
