import uuid

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.api.filters.entries import EntryFilter
from apps.opds.schema import (
    ACQUISITION_FEED_TYPE,
    NAVIGATION_FEED_TYPE,
    Link,
    LinkType,
    OpenSearchDescription,
    OpenSearchLink,
)
from apps.opds.services.entry_search import EntrySearchService
from apps.opds.services.feeds import AcquisitionFeed
from apps.opds.services.paging import FeedPage
from apps.opds.views.base import OpdsCatalogView
from apps.opds.views.feed import _page_entries

OPENSEARCH_TYPE = "application/opensearchdescription+xml"


class SearchDescriptorView(OpdsCatalogView):
    def get(self, request, catalog_name: str):
        # OpenSearch 1.1: the reader substitutes the user's words for
        # `{searchTerms}`. Without it a client has nowhere to put the query.
        template_items = {"query": "searchTerms"}

        if "feed_id" in request.GET:
            template_items["feed_id"] = request.GET["feed_id"]

        for name, filter_item in EntryFilter.base_filters.items():
            if "opensearch_template" in filter_item.extra:
                template_items[name] = filter_item.extra["opensearch_template"]

        url = OpenSearchLink(
            base_path=request.build_absolute_uri(
                reverse("opds:search", kwargs={"catalog_name": self.catalog.url_name})
            ),
            template_items=template_items,
        )

        descriptor = OpenSearchDescription(
            short_name=self.catalog.title[:16],
            description=_("Search %s") % (self.catalog.title,),
            url=url,
        )

        return HttpResponse(
            descriptor.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type=OPENSEARCH_TYPE,
        )


class SearchView(OpdsCatalogView):
    def get(self, request, catalog_name: str):
        entries = EntrySearchService.search(self.catalog, request)
        # Paging needs a total order: keep the relevance ranking when there
        # is one, and break ties (or order an unranked result) newest first.
        ordering = [field for field in entries.query.order_by if field not in ("pk", "-pk")] or ["-created_at"]
        page = FeedPage.resolve(request, entries.order_by(*ordering, "-pk"), settings.EVILFLOWERS_OPDS_PAGE_SIZE)

        query = request.GET.get("query", "")
        path = reverse("opds:search", kwargs={"catalog_name": catalog_name})
        canonical = request.build_absolute_uri(request.get_full_path())

        result = AcquisitionFeed(
            # Atom ids are permanent: the same search names the same feed.
            f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, canonical)}",
            title=_("Search results in %s") % (self.catalog.title,) if not query else _("Search: %s") % (query,),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            entries=_page_entries(page),
            links=[
                Link(rel=LinkType.SELF, href=request.get_full_path(), type=ACQUISITION_FEED_TYPE),
                Link(
                    rel=LinkType.START,
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type=NAVIGATION_FEED_TYPE,
                ),
                Link(
                    rel=LinkType.SEARCH,
                    href=reverse("opds:search-descriptor", kwargs={"catalog_name": catalog_name}),
                    type=OPENSEARCH_TYPE,
                ),
                *page.links(request, path),
            ],
            request=request,
            user=request.user,
        )

        return HttpResponse(result.to_xml(), content_type=ACQUISITION_FEED_TYPE)
