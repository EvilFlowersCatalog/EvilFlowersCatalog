"""Background work of the library graph (#154)."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

#: Seconds the nightly rebuild may take (every active run is projected again).
REBUILD_TIME_LIMIT = 2 * 60 * 60


@shared_task(time_limit=REBUILD_TIME_LIMIT, soft_time_limit=REBUILD_TIME_LIMIT - 60)
def rebuild_graph() -> dict:
    """Drop and re-project the `library` graph from the relational tables (`manage.py graph_rebuild`).

    Catches what the incremental projection missed: a deleted entry whose graph delete failed
    (`graph.signals`), a summary written while the graph was unavailable. Skipped, not failed, where
    Apache AGE is not installed.
    """
    from apps.graph import age, projection

    if not age.create_graph():
        logger.info("graph.rebuild skipped: Apache AGE is not installed in this database")
        return {"skipped": "age not installed"}
    totals = projection.rebuild()
    logger.info("graph.rebuild projected %s", totals)
    return totals
