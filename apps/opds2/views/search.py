from apps.api.filters.entries import EntryFilter
from apps.core.models import Entry
from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class SearchView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        queryset = Entry.objects.filter(catalog=self.catalog)
        entries = EntryFilter(request.GET, queryset=queryset, request=request).qs

        page = int(request.GET.get("page", 1))
        query = request.GET.get("query", "")
        feed = FeedBuilder.build_publication_feed(
            entries, self.catalog, request, page=page, title=f"Search: {query}" if query else "Search"
        )
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))
