from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse

from apps.api.filters.entries import EntryFilter
from apps.opds.schema import OpenSearchLink
from apps.opds.views.base import OpdsCatalogView


class SearchDescriptorView(OpdsCatalogView):
    def get(self, request, catalog_name: str):
        template_items = {}

        if "feed_id" in request.GET:
            template_items["feed_id"] = request.GET["feed_id"]

        for name, filter_item in EntryFilter.base_filters.items():
            if "opensearch_template" in filter_item.extra:
                template_items[name] = filter_item.extra["opensearch_template"]

        result = OpenSearchLink(
            base_path=request.build_absolute_uri(
                reverse("opds:search", kwargs={"catalog_name": self.catalog.url_name})
            ),
            template_items=template_items,
        )

        return HttpResponse(
            result.to_xml(
                pretty_print=settings.DEBUG,
                encoding="UTF-8",
                standalone=True,
                skip_empty=True,
            ),
            content_type="application/opensearchdescription+xml",
        )
