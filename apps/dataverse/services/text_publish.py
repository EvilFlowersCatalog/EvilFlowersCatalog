"""
Text-service publish client.

IP-008 Phase 4 / Phase 6 F2: relocated from `apps/api/services/
text_service_client.py`. The old client had no idempotency key, no
queue override, no timeout. This version:

- Uses an explicit `task_id=text:index:{acquisition_pk}` so duplicate
  publishes get coalesced by Celery (idempotent re-enqueue).
- Targets the `evilflowers_text_worker` queue by name.
- Logs structured fields so operators can grep publishes by acquisition.

A re-export shim `apps.api.services.text_service_client.TextServiceClient`
forwards to this class so legacy callers keep working for one release.
"""

import logging
from typing import Any, Dict, Optional

from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)


_TEXT_WORKER_TASK_NAME = "evilflowers_text_worker.process_pdf"
_TEXT_WORKER_QUEUE = "evilflowers_text_worker"


class TextServiceClient:
    """Enqueue a text-extraction Celery task for the text-service worker.

    Idempotency: the explicit `task_id` keyed by acquisition (or entry +
    source) means a duplicate publish gets dropped by Celery rather than
    running PDF extraction twice (IP-008 Phase 6 F2).
    """

    def __init__(self):
        pass

    def process_acquisition(self, source: str, entry_id: str) -> Optional[Dict[str, Any]]:
        """Enqueue async PDF processing.

        Args:
            source: relative file path from storage root
            entry_id: the acquisition/entry id used for indexing
        """
        if not source or not entry_id:
            logger.error("Invalid parameters for text processing: source=%r entry_id=%r", source, entry_id)
            return None

        task_id = f"text:index:{entry_id}"

        try:
            result = app.send_task(
                _TEXT_WORKER_TASK_NAME,
                args=[source, str(entry_id)],
                queue=_TEXT_WORKER_QUEUE,
                task_id=task_id,
            )
        except Exception:
            logger.exception("Failed to publish text-service task entry_id=%s source=%s", entry_id, source)
            return None

        publish_id = getattr(result, "id", task_id)
        logger.info(
            "Published text-service task: task_id=%s queue=%s entry_id=%s source=%s",
            publish_id,
            _TEXT_WORKER_QUEUE,
            entry_id,
            source,
        )
        return {"task_id": publish_id, "source": source, "entry_id": entry_id}
