"""Background work of the content app: Crossref licences (#177)."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def refresh_crossref_rights(entry_ids=None, missing_only: bool = True) -> dict:
    """Look the DOI of these entries (or of every entry with a DOI) up in Crossref and store its licence.

    `missing_only` skips entries that already have rights. A manual edit is never overwritten.
    """
    from apps.content import rights
    from apps.core.models import Entry

    entries = Entry.objects.filter(identifiers__has_key="doi")
    if entry_ids:
        entries = entries.filter(pk__in=list(entry_ids))
    if missing_only:
        entries = entries.filter(rights__isnull=True)
    counts = {"checked": 0, "updated": 0}
    for entry in entries.only("id", "identifiers", "rights").iterator(chunk_size=100):
        counts["checked"] += 1
        if rights.refresh_crossref(entry):
            counts["updated"] += 1
    logger.info("content.crossref_rights checked=%s updated=%s", counts["checked"], counts["updated"])
    return counts
