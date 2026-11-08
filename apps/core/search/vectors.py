"""
Entry vectors (IP-021): `entries.embedding` from title, authors, categories and summary.

Embedding is an HTTP call, so it never runs in the request or in a signal: changed entries are
collected per transaction and one Celery task (`apps.core.tasks.embed_entries`) runs after commit.
"""

import logging
import threading
from typing import Iterable, List

from django.db import connection, transaction

from apps.core.search import embeddings

logger = logging.getLogger(__name__)

_pending = threading.local()

DOCUMENT_SQL = """
SELECT id, title, search_authors, search_subjects, search_summary
FROM entries
WHERE id = ANY(%s)
"""


def document(title, authors, subjects, summary) -> str:
    """The text an entry is embedded from: what a reader would search it by, most important first."""
    return "\n".join(part.strip() for part in (title, authors, subjects, summary) if part and part.strip())


def embed_entries(entry_ids: Iterable) -> int:
    """Embed the given entries now (Celery task body and backfill). Returns how many were stored."""
    ids = [str(entry_id) for entry_id in entry_ids]
    if not ids or not embeddings.enabled():
        return 0

    with connection.cursor() as cursor:
        cursor.execute(DOCUMENT_SQL, [ids])
        rows = cursor.fetchall()
    if not rows:
        return 0

    model = embeddings.model_id()
    vectors = embeddings.embed_documents([document(*row[1:]) for row in rows])
    with connection.cursor() as cursor:
        for (entry_id, *_), vector in zip(rows, vectors):
            cursor.execute(
                "UPDATE entries SET embedding = %s::halfvec, embedding_model = %s WHERE id = %s",
                [_literal(vector), model, entry_id],
            )
    return len(rows)


def _literal(vector: List[float]) -> str:
    return "[" + ",".join(f"{value:.6g}" for value in vector) + "]"


def schedule(entry_ids: Iterable) -> None:
    """Queue embedding of these entries after the current transaction commits (one task per transaction)."""
    if not embeddings.enabled():
        return
    ids = {str(entry_id) for entry_id in entry_ids if entry_id is not None}
    if not ids:
        return

    pending = getattr(_pending, "ids", None)
    if pending is not None and _flush_queued():
        pending.update(ids)
        return
    # A rolled-back transaction drops its callbacks; its ids are re-queued with these.
    _pending.ids = (pending or set()) | ids
    # Outside a transaction `on_commit` runs the callback at once.
    transaction.on_commit(_flush)


def _flush_queued() -> bool:
    return any(callback is _flush for _savepoints, callback, *_rest in connection.run_on_commit)


def _flush() -> None:
    from apps.core.tasks import embed_entries as task

    ids = getattr(_pending, "ids", None) or set()
    _pending.ids = None
    if ids:
        try:
            task.delay(sorted(ids))
        except Exception:  # noqa: BLE001 — a broker outage must not fail the write that triggered it
            logger.exception("Embedding of %d entries not queued; run `manage.py embed_entries`", len(ids))


__all__ = ["document", "embed_entries", "schedule"]
