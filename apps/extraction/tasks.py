"""After a run becomes active (IP-030): its cross-references, figure documents and graph projection."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def post_process_run(run_id: str) -> dict:
    """References, then figure documents and vectors (they use the references), then the AGE graph."""
    from apps.extraction import references, regions
    from apps.extraction.models import ExtractionRun

    run = ExtractionRun.objects.filter(pk=run_id, is_active=True).select_related("acquisition").first()
    if run is None:
        return {}
    report = {"references": references.build(run)}
    try:
        report["regions"] = regions.index_run(run)
    except Exception:
        # TEI down: the documents are set, the vectors follow with `link_extraction`.
        logger.exception("Region vectors of run %s failed", run_id)
    try:
        from apps.graph import projection

        report["graph"] = projection.project_run(run)
    except Exception:
        logger.exception("Graph projection of run %s failed", run_id)
    return report


def queue_post_process(run_id) -> None:
    """Queue `post_process_run`; a broker that is down costs the step, never the ingestion request."""
    try:
        post_process_run.delay(str(run_id))
    except Exception:
        logger.exception("Could not queue post-processing of run %s; run `link_extraction`", run_id)
