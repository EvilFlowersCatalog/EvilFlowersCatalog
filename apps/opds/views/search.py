import uuid

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.api.filters.entries import EntryFilter
from apps.opds.schema import Link, LinkType, OpenSearchDescription, OpenSearchLink
from apps.opds.services.entry_search import EntrySearchService
from apps.opds.services.feeds import AcquisitionFeed
from apps.opds.views.base import OpdsCatalogView


class SearchDescriptorView(OpdsCatalogView):
    def get(self, request, catalog_name: str):
        template_items = {}

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
            content_type="application/opensearchdescription+xml",
        )


class SearchView(OpdsCatalogView):
    def get(self, request, catalog_name: str):
        entries = EntrySearchService.search(self.catalog, request)
        query = request.GET.get("query", "")

        result = AcquisitionFeed(
            f"urn:uuid:{uuid.uuid4()}",
            title=_("Search results in %s") % (self.catalog.title,) if not query else _("Search: %s") % (query,),
            author=self.catalog.creator,
            updated_at=self.catalog.touched_at,
            qs=entries,
            links=[
                Link(
                    rel=LinkType.SELF,
                    href=reverse("opds:search", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=acquisition",
                ),
                Link(
                    rel=LinkType.START,
                    href=reverse("opds:root", kwargs={"catalog_name": catalog_name}),
                    type="application/atom+xml;profile=opds-catalog;kind=navigation",
                ),
                Link(
                    rel=LinkType.SEARCH,
                    href=reverse("opds:search-descriptor", kwargs={"catalog_name": catalog_name}),
                    type="application/opensearchdescription+xml",
                ),
            ],
        )

        return HttpResponse(
            result.to_xml(),
            content_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )
