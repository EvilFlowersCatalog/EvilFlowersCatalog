"""Background work on catalog entries."""

from celery import shared_task


@shared_task
def store_table_of_contents(entry_id: str) -> None:
    """Store the outline and page count of the entry's PDF (entry detail "Obsah")."""
    from apps.core.services.table_of_contents import store_table_of_contents as store

    store(entry_id)
