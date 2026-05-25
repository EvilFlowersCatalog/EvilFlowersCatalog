"""
SearchServiceClient — HTTP wrapper around the evilflowers-search-service.

IP-008 Phase 6 F1: the search service indexes every PDF on upload but
nothing reads from it. This client lets OPDS 2.0 search consult the
keyword (`POST /search/elasticsearch`) and semantic
(`POST /search/semantic`) endpoints.

Failure mode: short timeout (10s, no retry — searches are interactive),
all exceptions surface as `SearchServiceUnavailable` so callers can
map to `502 + Retry-After`.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SECONDS = 10
_DEFAULT_RESULT_LIMIT = 50


class SearchServiceError(Exception):
    """Base exception for search-service failures."""


class SearchServiceUnavailable(SearchServiceError):
    """The search service is unreachable or timed out.

    Maps to HTTP 502 + Retry-After at the view layer.
    """


class SearchServiceBadResponse(SearchServiceError):
    """The search service returned a non-2xx or unparseable payload."""


class SearchServiceClient:
    """Thin client over evilflowers-search-service.

    The service is documented in `docs/evilflowers-search-service.pdf`
    (or `docs/search_service_reference.pdf` — see the README). Both
    endpoints take `{"query": str, "document_ids": [uuid, ...],
    "limit": int}` and return `{"results": [{"document_id": uuid, ...},
    ...]}`.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: int = _DEFAULT_TIMEOUT_SECONDS):
        self.base_url = (base_url or getattr(settings, "SEARCH_SERVICE_URL", "")).rstrip("/")
        if not self.base_url:
            raise SearchServiceUnavailable("SEARCH_SERVICE_URL is not configured")
        self.timeout = timeout

    # ----- public --------------------------------------------------------

    def keyword(
        self,
        query: str,
        *,
        document_ids: Iterable[str],
        limit: int = _DEFAULT_RESULT_LIMIT,
    ) -> List[str]:
        """Run a keyword (Elasticsearch) search scoped to the given
        document allow-list. Returns the matching `document_id` strings.
        """
        return self._search("/search/elasticsearch", query, document_ids, limit)

    def semantic(
        self,
        query: str,
        *,
        document_ids: Iterable[str],
        limit: int = _DEFAULT_RESULT_LIMIT,
    ) -> List[str]:
        """Run a semantic (vector) search scoped to the document allow-list."""
        return self._search("/search/semantic", query, document_ids, limit)

    # ----- internal -------------------------------------------------------

    def _search(self, path: str, query: str, document_ids: Iterable[str], limit: int) -> List[str]:
        ids = [str(doc_id) for doc_id in document_ids]
        if not ids:
            # No documents in the requester's allow-list — nothing to find.
            # Short-circuit so we don't waste an upstream call.
            return []

        url = f"{self.base_url}{path}"
        payload = {"query": query, "document_ids": ids, "limit": limit}

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
        except requests.Timeout as exc:
            raise SearchServiceUnavailable(f"search-service timed out at {url}") from exc
        except requests.RequestException as exc:
            raise SearchServiceUnavailable(f"search-service unreachable at {url}: {exc}") from exc

        if response.status_code in range(500, 600):
            raise SearchServiceUnavailable(
                f"search-service returned {response.status_code} body={response.text[:200]}"
            )
        if response.status_code != 200:
            raise SearchServiceBadResponse(
                f"search-service returned {response.status_code} body={response.text[:200]}"
            )

        try:
            body = response.json() or {}
        except ValueError as exc:
            raise SearchServiceBadResponse(f"search-service returned invalid JSON: {exc}") from exc

        results = body.get("results")
        if not isinstance(results, list):
            return []

        out: List[str] = []
        for item in results:
            doc_id = item.get("document_id") if isinstance(item, dict) else None
            if doc_id:
                out.append(str(doc_id))
        return out
