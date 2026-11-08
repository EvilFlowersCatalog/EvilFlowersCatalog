"""
Shared OPDS search query layer, consumed by OPDS 1.2 and OPDS 2.0.

Every mode runs in the catalog's PostgreSQL (IP-021; the search-service is gone):

- `catalog` (default): hybrid — the words (BM25 / full text) fused with the entries closest in meaning;
- `keyword`: the words only;
- `semantic`: meaning only (entry vectors; empty without TEI).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from django.db.models import QuerySet

from apps.api.filters.entries import EntryFilter
from apps.core.models import Catalog, Entry
from apps.core.search.query import Channel


class SearchMode(str, Enum):
    CATALOG = "catalog"
    KEYWORD = "keyword"
    SEMANTIC = "semantic"

    @classmethod
    def parse(cls, raw: Optional[str]) -> "SearchMode":
        """Coerce a query-string mode into the enum; unknown / missing values mean `CATALOG`."""
        if not raw:
            return cls.CATALOG
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return cls.CATALOG

    @property
    def channels(self) -> tuple:
        return {
            SearchMode.CATALOG: Channel.BOTH,
            SearchMode.KEYWORD: (Channel.LEXICAL,),
            SearchMode.SEMANTIC: (Channel.VECTOR,),
        }[self]


class EntrySearchService:
    @staticmethod
    def search(catalog: Catalog, request, *, mode: SearchMode = SearchMode.CATALOG) -> QuerySet[Entry]:
        return EntrySearchService.catalog_filter(catalog, request, mode=mode).qs

    @staticmethod
    def catalog_filter(catalog: Catalog, request, mode: SearchMode = SearchMode.CATALOG) -> EntryFilter:
        """The catalog's filter: its `qs` are the results, its `search` the resolved search."""
        return EntryFilter(
            request.GET,
            queryset=Entry.objects.filter(catalog=catalog),
            request=request,
            search_channels=mode.channels,
        )

    @staticmethod
    def catalog_facets(catalog: Catalog, request, entry_filter: Optional[EntryFilter] = None) -> dict:
        """Facet counts within `catalog` for the current filters (IP-020), as `EntryFacetService` returns them."""
        from apps.api.services.facets import EntryFacetService, OPDS_DIMENSIONS

        params = request.GET.copy()
        params["facet"] = ",".join(OPDS_DIMENSIONS)
        return EntryFacetService(
            request,
            params,
            queryset=Entry.objects.filter(catalog=catalog),
            search=entry_filter.search if entry_filter is not None else None,
        ).compute()
