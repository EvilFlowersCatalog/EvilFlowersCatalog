"""
Celery tasks for the Dataverse integration.

`resume_workflow` (IP-008 Phase 4 D2): replaces the legacy
`threading.Thread` daemon with a real Celery task. Autoretries on
transient HTTP failures with exponential backoff capped at 30s, matching
the original retry envelope.
"""

import logging
from typing import Optional

from celery import shared_task

from apps.dataverse.exceptions import DataverseError, DataverseTransientError
from apps.dataverse.services.client import DataverseClient

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 30


@shared_task(
    bind=True,
    autoretry_for=(DataverseTransientError,),
    retry_backoff=True,
    retry_backoff_max=_MAX_BACKOFF_SECONDS,
    retry_jitter=True,
    name="apps.dataverse.tasks.resume_workflow",
)
def resume_workflow(
    self,
    *,
    dataverse_base_url: str,
    dataverse_token: str,
    invocation_id: str,
    resume_base_url: Optional[str] = None,
    max_attempts: int = 10,
    initial_delay: float = 1.0,  # noqa: ARG001 — kept for caller signature parity
) -> None:
    """Resume a Dataverse workflow invocation.

    Issues `POST {resume_base_url}/api/workflows/{invocation_id}` with
    body "OK". On a 404 the workflow is not yet recorded on the
    Dataverse side; we autoretry until `self.request.retries` exceeds
    `max_attempts`.

    On 2xx we log success and return.
    On any other status we log and stop (non-retryable).
    """
    client = DataverseClient(dataverse_base_url, dataverse_token)
    target_base = (resume_base_url or dataverse_base_url).rstrip("/")

    logger.info(
        "Resuming Dataverse workflow invocation %s (attempt %s/%s)",
        invocation_id,
        self.request.retries + 1,
        max_attempts,
    )

    try:
        response = client.resume_workflow(target_base, invocation_id)
    except DataverseTransientError as exc:
        # Convert to a retry within the autoretry envelope, unless we've
        # exhausted the configured budget.
        if self.request.retries + 1 >= max_attempts:
            logger.error(
                "Dataverse workflow resume gave up after %s attempts: %s",
                self.request.retries + 1,
                exc,
            )
            return
        raise

    if 200 <= response.status_code < 300:
        logger.info(
            "Dataverse workflow resume accepted: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return

    if response.status_code == 404 and self.request.retries + 1 < max_attempts:
        logger.info(
            "Dataverse workflow invocation is not ready yet: status=404 body=%s",
            response.text[:500],
        )
        # Raise a transient error so Celery autoretry kicks in with backoff.
        raise DataverseTransientError(f"workflow {invocation_id} not ready yet")

    logger.warning(
        "Dataverse workflow resume returned an error: status=%s body=%s",
        response.status_code,
        response.text[:1000],
    )


__all__ = ["resume_workflow"]
