"""
Text embeddings from the shared Text Embeddings Inference server (`tei-embed`, IP-021).

The model is `intfloat/multilingual-e5-large-instruct` (1024 dimensions, 512 tokens). Client contract
(evilflowers-knowledge `Proposals/Search-Embeddings`, `Cookbooks/Shared-Inference`):

1. Queries carry an instruction (`Instruct: …\\nQuery: …`); documents (entries, paragraphs) are plain.
2. At most 32 inputs per request; longer texts are truncated by TEI (`truncate: true`).
3. The model id and revision (`GET /info`) travel with every stored vector: vectors of two models are
   never compared.
4. A query waits at most its caller's budget: `EVILFLOWERS_EMBEDDING_QUERY_TIMEOUT` (0.3 s) for a catalog
   search, 2 s for the assistant and passage search; 60 s for a batch, 3 retries with backoff on 429/503.
   Batches also retry timeouts and connection errors; a reader's query does not, and after TEI failed
   (down, overloaded, or no answer in the full 2 s) queries skip TEI for `UNAVAILABLE_SECONDS`. A search
   that only ran out of its short budget degrades alone: TEI is busy, not down (#134).
5. Type-ahead never calls TEI: it uses a query vector only when a search already cached it.

Unconfigured (`EVILFLOWERS_EMBEDDING_URL` empty) the client is disabled and search stays lexical.
"""

import hashlib
import logging
import time
from typing import List, Optional, Sequence

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

BATCH_SIZE = 32
#: Budget of a query whose caller names none (assistant, passage search); catalog search uses `search_budget()`.
QUERY_TIMEOUT = 2
BATCH_TIMEOUT = 60
RETRIES = 3
RETRY_STATUSES = (429, 503)
#: After a reader's query could not be embedded, skip TEI for this long (search degrades to lexical).
UNAVAILABLE_KEY = "evilflowers:embedding:unavailable"
UNAVAILABLE_SECONDS = 30
#: Query vectors are cached this long (paging, facets, type-ahead).
QUERY_CACHE_SECONDS = 600
#: A search that went without vectors stays so this long, so its later pages match the first (#134).
DEGRADED_SECONDS = QUERY_CACHE_SECONDS

#: Instructions for e5-instruct queries, per task.
INSTRUCTIONS = {
    "entries": "Given a library search query, retrieve relevant books",
    "passages": "Given a library search query, retrieve relevant book passages",
    "similar": "Given a passage of a book, retrieve passages that discuss the same topic",
}


class EmbeddingError(Exception):
    """TEI is unconfigured, unreachable or answered with an error."""


class EmbeddingUnavailable(EmbeddingError):
    """TEI did not answer (timeout, connection error) or stayed overloaded (429/503) after the retries."""


class EmbeddingTimeout(EmbeddingUnavailable):
    """TEI did not answer a query within the caller's budget."""


def enabled() -> bool:
    return bool(getattr(settings, "EVILFLOWERS_EMBEDDING_URL", ""))


def search_budget() -> float:
    """Seconds a catalog search waits for its query vector (`EVILFLOWERS_EMBEDDING_QUERY_TIMEOUT`)."""
    return float(getattr(settings, "EVILFLOWERS_EMBEDDING_QUERY_TIMEOUT", 0.3))


def _url(path: str) -> str:
    return f"{settings.EVILFLOWERS_EMBEDDING_URL.rstrip('/')}{path}"


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if getattr(settings, "EVILFLOWERS_EMBEDDING_API_KEY", ""):
        headers["Authorization"] = f"Bearer {settings.EVILFLOWERS_EMBEDDING_API_KEY}"
    # Compose containers reach tei-embed through the internal Envoy listener, routed by Host
    # (`http://192.168.122.227:8081` + `Host: embed.elvira.internal`, evilflowers-knowledge Shared-Inference).
    if getattr(settings, "EVILFLOWERS_EMBEDDING_HOST", ""):
        headers["Host"] = settings.EVILFLOWERS_EMBEDDING_HOST
    return headers


def _request(method: str, path: str, *, timeout: float, json=None, retry_errors: bool = True):
    """Call TEI, retrying 429/503 with backoff.

    A timeout or connection error is retried only when `retry_errors` (batches). A reader's query
    is not: four 2 s timeouts plus backoff held `GET /api/v1/entries?query=` for ~9 s while TEI was slow.
    """
    if not enabled():
        raise EmbeddingError("EVILFLOWERS_EMBEDDING_URL is not set")
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            response = requests.request(method, _url(path), headers=_headers(), json=json, timeout=timeout)
        except requests.RequestException as error:
            if not retry_errors:
                if isinstance(error, requests.Timeout):
                    raise EmbeddingTimeout(str(error)) from error
                raise EmbeddingUnavailable(str(error)) from error
            last_error = error
        else:
            if response.status_code not in RETRY_STATUSES:
                if response.status_code >= 400:
                    raise EmbeddingError(f"TEI {path} answered {response.status_code}: {response.text[:200]}")
                return response.json()
            last_error = EmbeddingError(f"TEI {path} answered {response.status_code}")
        if attempt < RETRIES:
            time.sleep(0.2 * 2**attempt)
    raise EmbeddingUnavailable(str(last_error))


MODEL_KEY = "evilflowers:embedding:model"


def known_model_id() -> Optional[str]:
    """`model_id()` without asking TEI: configured or cached, else None."""
    return getattr(settings, "EVILFLOWERS_EMBEDDING_MODEL", "") or cache.get(MODEL_KEY) or None


def model_id(timeout: float = QUERY_TIMEOUT) -> str:
    """`model@revision` of the running TEI (cached for an hour); the provenance of every vector."""
    known = known_model_id()
    if known:
        return known
    info = _request("GET", "/info", timeout=timeout, retry_errors=False)
    value = f"{info.get('model_id')}@{info.get('model_sha') or 'unknown'}"
    cache.set(MODEL_KEY, value, 3600)
    return value


def query_text(text: str, task: str = "entries") -> str:
    return f"Instruct: {INSTRUCTIONS[task]}\nQuery: {text.strip()}"


def _query_key(model: str, prompt: str) -> str:
    digest = hashlib.sha256((model + "\n" + prompt).encode()).hexdigest()
    return f"evilflowers:embedding:query:{digest}"


def _degraded_key(prompt: str) -> str:
    return f"evilflowers:embedding:degraded:{hashlib.sha256(prompt.encode()).hexdigest()}"


def cached_query(text: str, task: str = "entries") -> Optional[List[float]]:
    """The query's vector if an earlier search cached it; never calls TEI."""
    if not enabled():
        return None
    model = known_model_id()
    if model is None:
        return None
    return cache.get(_query_key(model, query_text(text, task)))


def embed_documents(texts: Sequence[str]) -> List[List[float]]:
    """Plain documents, in batches of at most 32, normalised."""
    vectors: List[List[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [text or " " for text in texts[start : start + BATCH_SIZE]]
        vectors.extend(
            _request(
                "POST",
                "/embed",
                timeout=BATCH_TIMEOUT,
                json={"inputs": batch, "normalize": True, "truncate": True},
            )
        )
    return vectors


def embed_query(text: str, task: str = "entries", *, timeout: float = QUERY_TIMEOUT) -> List[float]:
    """
    One reader's query with the task instruction; cached for 10 minutes (paging, facets). `timeout` is
    the whole budget: reading the model from `/info` (when not cached) and embedding share it.
    """
    deadline = time.monotonic() + timeout
    prompt = query_text(text, task)
    key = _query_key(model_id(timeout=timeout), prompt)
    vector = cache.get(key)
    if vector is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EmbeddingTimeout("the budget was spent reading /info")
        vector = _request(
            "POST",
            "/embed",
            timeout=remaining,
            json={"inputs": [prompt], "normalize": True, "truncate": True},
            retry_errors=False,
        )[0]
        cache.set(key, vector, QUERY_CACHE_SECONDS)
    return vector


def try_embed_query(
    text: str,
    task: str = "entries",
    *,
    timeout: float = QUERY_TIMEOUT,
    cached_only: bool = False,
    stable: bool = False,
) -> Optional[List[float]]:
    """
    `embed_query`, or None (logged) when TEI is unconfigured, down or slower than `timeout`: callers
    degrade to lexical search. A cached vector is used even while the breaker is open.

    `cached_only` (type-ahead): never call TEI, use the vector only when a search already cached it.
    `stable` (catalog search): a query that went without vectors once stays so for `DEGRADED_SECONDS`,
    so page 2 of a result is computed like page 1 (#134).
    """
    if not enabled():
        return None
    vector = cached_query(text, task)
    if vector is not None or cached_only:
        return vector

    degraded_key = _degraded_key(query_text(text, task))
    if cache.get(UNAVAILABLE_KEY) or (stable and cache.get(degraded_key)):
        if stable:
            cache.set(degraded_key, True, DEGRADED_SECONDS)
        return None
    try:
        return embed_query(text, task, timeout=timeout)
    except EmbeddingError as error:
        # Searches in the next UNAVAILABLE_SECONDS stay lexical instead of each waiting for the same
        # timeout. A 4xx about this one query says nothing about TEI, and neither does a short budget
        # running out (TEI is busy, not down): neither opens the breaker.
        budget_only = isinstance(error, EmbeddingTimeout) and timeout < QUERY_TIMEOUT
        if isinstance(error, EmbeddingUnavailable) and not budget_only:
            cache.set(UNAVAILABLE_KEY, True, UNAVAILABLE_SECONDS)
        if stable:
            cache.set(degraded_key, True, DEGRADED_SECONDS)
        logger.warning("Query not embedded, searching without vectors: %s", error)
        return None


__all__ = [
    "EmbeddingError",
    "EmbeddingTimeout",
    "EmbeddingUnavailable",
    "cached_query",
    "embed_documents",
    "embed_query",
    "enabled",
    "known_model_id",
    "model_id",
    "query_text",
    "search_budget",
    "try_embed_query",
]
