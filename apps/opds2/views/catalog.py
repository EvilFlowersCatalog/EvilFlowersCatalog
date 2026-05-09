from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class CatalogView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        feed = FeedBuilder.build_catalog_feed(self.catalog, request)
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))
