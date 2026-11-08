"""
Text embeddings from the shared Text Embeddings Inference server (`tei-embed`, IP-021).

The model is `intfloat/multilingual-e5-large-instruct` (1024 dimensions, 512 tokens). Client contract
(evilflowers-knowledge `Proposals/Search-Embeddings`, `Cookbooks/Shared-Inference`):

1. Queries carry an instruction (`Instruct: …\\nQuery: …`); documents (entries, paragraphs) are plain.
2. At most 32 inputs per request; longer texts are truncated by TEI (`truncate: true`).
3. The model id and revision (`GET /info`) travel with every stored vector: vectors of two models are
   never compared.
4. 2 s timeout for a query, 60 s for a batch, 3 retries with backoff on 429/503.

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
QUERY_TIMEOUT = 2
BATCH_TIMEOUT = 60
RETRIES = 3
RETRY_STATUSES = (429, 503)

#: Instructions for e5-instruct queries, per task.
INSTRUCTIONS = {
    "entries": "Given a library search query, retrieve relevant books",
    "passages": "Given a library search query, retrieve relevant book passages",
    "similar": "Given a passage of a book, retrieve passages that discuss the same topic",
}


class EmbeddingError(Exception):
    """TEI is unconfigured, unreachable or answered with an error."""


def enabled() -> bool:
    return bool(getattr(settings, "EVILFLOWERS_EMBEDDING_URL", ""))


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


def _request(method: str, path: str, *, timeout: float, json=None):
    if not enabled():
        raise EmbeddingError("EVILFLOWERS_EMBEDDING_URL is not set")
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            response = requests.request(method, _url(path), headers=_headers(), json=json, timeout=timeout)
        except requests.RequestException as error:
            last_error = error
        else:
            if response.status_code not in RETRY_STATUSES:
                if response.status_code >= 400:
                    raise EmbeddingError(f"TEI {path} answered {response.status_code}: {response.text[:200]}")
                return response.json()
            last_error = EmbeddingError(f"TEI {path} answered {response.status_code}")
        if attempt < RETRIES:
            time.sleep(0.2 * 2**attempt)
    raise EmbeddingError(str(last_error))


def model_id() -> str:
    """`model@revision` of the running TEI (cached for an hour); the provenance of every vector."""
    configured = getattr(settings, "EVILFLOWERS_EMBEDDING_MODEL", "")
    if configured:
        return configured
    cached = cache.get("evilflowers:embedding:model")
    if cached:
        return cached
    info = _request("GET", "/info", timeout=QUERY_TIMEOUT)
    value = f"{info.get('model_id')}@{info.get('model_sha') or 'unknown'}"
    cache.set("evilflowers:embedding:model", value, 3600)
    return value


def query_text(text: str, task: str = "entries") -> str:
    return f"Instruct: {INSTRUCTIONS[task]}\nQuery: {text.strip()}"


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


def embed_query(text: str, task: str = "entries") -> List[float]:
    """One reader's query with the task instruction; cached for 10 minutes (paging, facets)."""
    prompt = query_text(text, task)
    digest = hashlib.sha256(f"{model_id()}\n{prompt}".encode()).hexdigest()
    key = f"evilflowers:embedding:query:{digest}"
    vector = cache.get(key)
    if vector is None:
        vector = _request(
            "POST", "/embed", timeout=QUERY_TIMEOUT, json={"inputs": [prompt], "normalize": True, "truncate": True}
        )[0]
        cache.set(key, vector, 600)
    return vector


def try_embed_query(text: str, task: str = "entries") -> Optional[List[float]]:
    """`embed_query`, or None (logged) when TEI is unconfigured or down: callers degrade to lexical search."""
    if not enabled():
        return None
    try:
        return embed_query(text, task)
    except EmbeddingError as error:
        logger.warning("Query not embedded, searching without vectors: %s", error)
        return None


__all__ = [
    "EmbeddingError",
    "embed_documents",
    "embed_query",
    "enabled",
    "model_id",
    "query_text",
    "try_embed_query",
]
