"""Cross-encoder reranking of passages on the shared `tei-rerank` (bge-reranker-v2-m3, multilingual) — #188 option B.

A Slovak question shares no words with an English paragraph, so passage search's word channel is empty and the page
depends on the vector ranking alone, which finds the book but often not the paragraph. A cross-encoder reads the
question and each paragraph together, across languages. Off unless `EVILFLOWERS_SEARCH_PASSAGE_RERANK_URL` is set;
any failure or a slow answer leaves the fused order as it was (logged), a search never fails on it.
"""

import logging
from typing import List, Optional, Sequence
from urllib.parse import urlparse

import requests
from django.conf import settings

from evil_flowers_catalog import tracing

logger = logging.getLogger(__name__)

#: Characters of a paragraph sent to the reranker; the model reads 512 tokens anyway (`truncate`).
TEXT_CHARS = 1500


def enabled() -> bool:
    return bool(getattr(settings, "EVILFLOWERS_SEARCH_PASSAGE_RERANK_URL", ""))


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    key = getattr(settings, "EVILFLOWERS_SEARCH_PASSAGE_RERANK_API_KEY", "") or getattr(
        settings, "EVILFLOWERS_EMBEDDING_API_KEY", ""
    )
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # The Compose path through the internal Envoy listener is routed by Host (`rerank.elvira.internal`).
    if getattr(settings, "EVILFLOWERS_SEARCH_PASSAGE_RERANK_HOST", ""):
        headers["Host"] = settings.EVILFLOWERS_SEARCH_PASSAGE_RERANK_HOST
    return headers


def scores(query: str, texts: Sequence[str], timeout: Optional[float] = None) -> Optional[List[float]]:
    """The relevance (0-1) of each text to `query`, in the order given; None when off, down or too slow."""
    if not enabled() or not texts or not query:
        return None
    timeout = settings.EVILFLOWERS_SEARCH_PASSAGE_RERANK_TIMEOUT if timeout is None else timeout
    url = f"{settings.EVILFLOWERS_SEARCH_PASSAGE_RERANK_URL.rstrip('/')}/rerank"
    with tracing.client_span("tei rerank", system="tei", server_address=urlparse(url).hostname) as span:
        span.set_attribute("evilflowers.rerank.texts", len(texts))
        try:
            response = requests.post(
                url,
                headers=_headers(),
                json={"query": query, "texts": [text[:TEXT_CHARS] for text in texts], "truncate": True},
                timeout=timeout,
            )
            response.raise_for_status()
            ranked = response.json()
        except (requests.RequestException, ValueError) as error:
            logger.warning("search.rerank_failed texts=%s error=%s", len(texts), error)
            return None
    out: List[Optional[float]] = [None] * len(texts)
    for item in ranked if isinstance(ranked, list) else []:
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < len(texts):
            out[index] = float(item.get("score") or 0.0)
    if any(score is None for score in out):
        logger.warning("search.rerank_incomplete texts=%s answered=%s", len(texts), sum(s is not None for s in out))
        return None
    return out
