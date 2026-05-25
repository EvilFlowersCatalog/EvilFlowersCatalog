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
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def schedule_workflow_resume(
    *,
    dataverse_base_url: str,
    dataverse_token: str,
    invocation_id: Optional[str],
) -> None:
    """Enqueue a Celery task to resume the Dataverse workflow.

    Gated by `DATAVERSE_RESUME_WORKFLOW=1` (operator opt-in). The task
    is deferred via `transaction.on_commit` by the caller; we only do
    the env validation here.
    """
    if not _env_flag("DATAVERSE_RESUME_WORKFLOW", default=False):
        return

    if not invocation_id:
        logger.warning("DATAVERSE_RESUME_WORKFLOW=1 but invocation_id is missing")
        return

    # Lazy import to keep the apps.dataverse import graph light.
    from django.db import transaction

    from apps.dataverse.tasks import resume_workflow

    max_attempts = _int_env("DATAVERSE_RESUME_WORKFLOW_ATTEMPTS", 10)
    initial_delay = _float_env("DATAVERSE_RESUME_WORKFLOW_INITIAL_DELAY", 1.0)
    resume_base_url = os.getenv("DV_WORKFLOW_RESUME_BASE", dataverse_base_url)

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
