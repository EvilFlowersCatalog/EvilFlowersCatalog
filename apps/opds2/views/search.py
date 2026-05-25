from apps.api.utils.parse import parse_int_query
from apps.opds.services.entry_search import EntrySearchService
from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class SearchView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        entries = EntrySearchService.search(self.catalog, request)

        page = parse_int_query(request, "page", default=1, min_value=1)
        query = request.GET.get("query", "")
        feed = FeedBuilder.build_publication_feed(
            entries, self.catalog, request, page=page, title=f"Search: {query}" if query else "Search"
        )
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))
