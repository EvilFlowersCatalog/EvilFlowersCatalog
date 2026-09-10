from http import HTTPStatus

from django.http import HttpResponse, JsonResponse

from apps.api.utils.parse import parse_int_query
from apps.opds.services.entry_search import EntrySearchService, SearchMode
from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class SearchView(Opds2CatalogView):
    """OPDS 2.0 search.

    Modes (IP-008 Phase 6 F1):
      - `mode=catalog` (default): DB-side `EntryFilter` — no network call.
      - `mode=keyword`:           evilflowers-search-service `/search/elasticsearch`.
      - `mode=semantic`:          evilflowers-search-service `/search/semantic`.

    Search-service-down responses surface as HTTP 502 + Retry-After so
    clients can fall back to `mode=catalog`.
    """

    def get(self, request, catalog_name: str):
        # Lazy import — the exception types live in the api app.
        from apps.api.services.search_service_client import (
            SearchServiceBadResponse,
            SearchServiceUnavailable,
        )

        mode = SearchMode.parse(request.GET.get("mode"))

        try:
            entries = EntrySearchService.search(self.catalog, request, mode=mode)
        except SearchServiceUnavailable as exc:
            return self._search_service_down(str(exc))
        except SearchServiceBadResponse as exc:
            # Bad upstream response — treat as 502 too, but without
            # Retry-After since retrying won't help if the schema is
            # broken.
            return self._search_service_down(str(exc), retry_after=None)

        page = parse_int_query(request, "page", default=1, min_value=1)
        query = request.GET.get("query", "")
        feed = FeedBuilder.build_publication_feed(
            entries, self.catalog, request, page=page, title=f"Search: {query}" if query else "Search"
        )
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))

    @staticmethod
    def _search_service_down(detail: str, retry_after: int | None = 10) -> HttpResponse:
        body = {
            "type": "/search-service-unavailable",
            "title": "Search service unavailable",
            "status": HTTPStatus.BAD_GATEWAY,
            "detail": detail,
        }
        response = JsonResponse(body, status=HTTPStatus.BAD_GATEWAY, content_type="application/problem+json")
        if retry_after is not None:
            response["Retry-After"] = str(retry_after)
        return response
