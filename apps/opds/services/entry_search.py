"""
Shared OPDS search query layer.

IP-007 shipped the catalog-DB search consumed by both OPDS 1.2 and OPDS 2.0.
IP-008 Phase 6 F1 extends it with optional `mode=keyword|semantic`
dispatch via the evilflowers-search-service.

Catalog scoping: the search service has no concept of catalog. We pass
the set of acquisition UUIDs that belong to the requested catalog as a
document allow-list, and post-hoc filter the results to be safe.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, List, Optional

from django.db.models import QuerySet

from apps.api.filters.entries import EntryFilter
from apps.core.models import Acquisition, Catalog, Entry


class SearchMode(str, Enum):
    CATALOG = "catalog"
    KEYWORD = "keyword"
    SEMANTIC = "semantic"

    @classmethod
    def parse(cls, raw: Optional[str]) -> "SearchMode":
        """Coerce a query-string mode into the enum.

        Unknown / missing values fall back to `CATALOG` so legacy
        clients keep working.
        """
        if not raw:
            return cls.CATALOG
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return cls.CATALOG


class EntrySearchService:
    """Shared catalog-DB search for OPDS 1.2 and OPDS 2.0.

    For OPDS 1.2 (Atom) the only supported mode is `CATALOG`; OPDS 2.0
    can opt into `KEYWORD` or `SEMANTIC` via the `?mode=` query param.
    Response shape diverges per profile; only the query layer is
    shared here.
    """

    @staticmethod
    def search(
        catalog: Catalog,
        request,
        *,
        mode: SearchMode = SearchMode.CATALOG,
    ) -> QuerySet[Entry]:
        if mode == SearchMode.CATALOG:
            return EntrySearchService._catalog_search(catalog, request)

        query = (request.GET.get("query") or "").strip()
        if not query:
            # Nothing to search for; return an empty queryset rather
            # than dispatching to the upstream service.
            return Entry.objects.none()

        # Lazy import — the search-service client is only needed for
        # keyword/semantic modes and pulls `requests` indirectly.
        from apps.api.services.search_service_client import (
            SearchServiceClient,
            SearchServiceError,
            SearchServiceUnavailable,
        )

        document_ids = list(EntrySearchService._catalog_acquisition_ids(catalog, request.user))
        if not document_ids:
            return Entry.objects.none()

        client = SearchServiceClient()
        if mode == SearchMode.KEYWORD:
            matched_ids = client.keyword(query, document_ids=document_ids)
        elif mode == SearchMode.SEMANTIC:
            matched_ids = client.semantic(query, document_ids=document_ids)
        else:
            raise SearchServiceError(f"Unsupported search mode: {mode}")

        if not matched_ids:
            return Entry.objects.none()

        # Defensive post-filter: only return entries whose acquisitions
        # are in the catalog. The allow-list above is the primary
        # scope, but a misconfigured search service that ignored the
        # allow-list would not leak across catalogs from here.
        return Entry.objects.filter(
            catalog=catalog,
            acquisitions__pk__in=matched_ids,
        ).distinct()

    # ----- internals -----------------------------------------------------

    @staticmethod
    def _catalog_search(catalog: Catalog, request) -> QuerySet[Entry]:
        queryset = Entry.objects.filter(catalog=catalog)
        return EntryFilter(request.GET, queryset=queryset, request=request).qs

    @staticmethod
    def _catalog_acquisition_ids(catalog: Catalog, user) -> Iterable[str]:
        """Return the Acquisition UUIDs that the search service may match
        against. Scope: same catalog, plus any access checks the
        requester would face via the normal entry filters.
        """
        qs = Acquisition.objects.filter(entry__catalog=catalog)
        # If the user is anonymous, only public-catalog content qualifies.
        # The shared EntryFilter already enforces this for catalog-mode
        # search; replicate the gate here for keyword/semantic.
        if user is None or not getattr(user, "is_authenticated", False):
            qs = qs.filter(entry__catalog__is_public=True)
        return list(qs.values_list("pk", flat=True))
