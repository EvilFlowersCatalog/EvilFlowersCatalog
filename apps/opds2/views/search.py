from apps.api.utils.parse import parse_int_query
from apps.opds.services.entry_search import EntrySearchService, SearchMode
from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class SearchView(Opds2CatalogView):
    """OPDS 2.0 search, in the catalog's database (IP-020, IP-021).

    Modes:
      - `mode=catalog` (default): hybrid — words (BM25 / full text) and meaning (entry vectors) fused.
      - `mode=keyword`:           the words only.
      - `mode=semantic`:          meaning only; empty when embeddings are not configured.
    """

    def get(self, request, catalog_name: str):
        mode = SearchMode.parse(request.GET.get("mode"))
        entry_filter = EntrySearchService.catalog_filter(self.catalog, request, mode=mode)
        entries = entry_filter.qs
        facets = EntrySearchService.catalog_facets(self.catalog, request, entry_filter)

        page = parse_int_query(request, "page", default=1, min_value=1)
        query = request.GET.get("query", "")
        feed = FeedBuilder.build_publication_feed(
            entries,
            self.catalog,
            request,
            page=page,
            title=f"Search: {query}" if query else "Search",
            facets=facets,
        )
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))
