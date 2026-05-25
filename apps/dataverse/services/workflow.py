"""
Workflow resume helpers.

The legacy view spawned a `threading.Thread` daemon to call the
Dataverse workflow resume callback with backoff. Under gevent/gunicorn
workers this becomes a green thread that dies on worker recycle, and
no retry surfaces in any tracker.

IP-008 Phase 4 D2: move this to a Celery task. The view publishes the
task on `transaction.on_commit` so a rolled-back prepublish doesn't
accidentally resume a Dataverse workflow. The task itself uses
Celery's autoretry on `DataverseTransientError`.
"""

import logging
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


def schedule_workflow_resume(
    *,
    dataverse_base_url: str,
    dataverse_token: str,
    invocation_id: Optional[str],
) -> None:
    """Enqueue a Celery task to resume the Dataverse workflow.

    Gated by `EVILFLOWERS_DATAVERSE_RESUME_WORKFLOW` (operator opt-in).
    The task is deferred via `transaction.on_commit` by the caller;
    we only do the configuration validation here.
    """
    if not settings.EVILFLOWERS_DATAVERSE_RESUME_WORKFLOW:
        return

    if not invocation_id:
        logger.warning("EVILFLOWERS_DATAVERSE_RESUME_WORKFLOW is enabled but invocation_id is missing")
        return

    # Lazy import to keep the apps.dataverse import graph light.
    from django.db import transaction

    from apps.dataverse.tasks import resume_workflow

    max_attempts = settings.EVILFLOWERS_DATAVERSE_RESUME_WORKFLOW_ATTEMPTS
    initial_delay = settings.EVILFLOWERS_DATAVERSE_RESUME_WORKFLOW_INITIAL_DELAY
    resume_base_url = settings.EVILFLOWERS_DATAVERSE_WORKFLOW_RESUME_BASE or dataverse_base_url

    transaction.on_commit(
        lambda: resume_workflow.apply_async(
            kwargs={
                "dataverse_base_url": dataverse_base_url,
                "dataverse_token": dataverse_token,
                "invocation_id": invocation_id,
                "resume_base_url": resume_base_url,
                "max_attempts": max_attempts,
                "initial_delay": initial_delay,
            },
            countdown=initial_delay if initial_delay > 0 else 0,
        )
    )
    logger.info("Scheduled Dataverse workflow resume for invocation %s", invocation_id)
