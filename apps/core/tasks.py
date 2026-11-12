"""Background work on catalog entries."""

from celery import shared_task


@shared_task
def store_table_of_contents(entry_id: str) -> None:
    """Store the outline and page count of the entry's PDF (entry detail "Obsah")."""
    from apps.core.services.table_of_contents import store_table_of_contents as store

    store(entry_id)


@shared_task(autoretry_for=(Exception,), retry_backoff=30, max_retries=5)
def embed_entries(entry_ids: list) -> int:
    """Embed entries through TEI for semantic search (IP-021); retried while TEI is down."""
    from apps.core.search.vectors import embed_entries as embed

    return embed(entry_ids)
