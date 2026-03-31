import logging
import os
from typing import Optional, Dict, Any
import uuid

from evil_flowers_catalog.celery import app
from django.conf import settings

logger = logging.getLogger("apps.api.services.text_service_client")


def _publish_task_to_redis(args: list) -> Optional[str]:
    """
    Publish a Celery task to Redis.
    Returns task_id if successful, None otherwise.
    """

    try:
        result = app.send_task("evilflowers_text_worker.process_pdf", args=args, queue="evilflowers_text_worker")

        task_id = result.id if result else None
        if task_id:
            logger.info(f"Published task to Redis: task_id={task_id}, queue=evilflowers_text_worker, args={args}")
        return task_id

    except Exception as e:
        logger.exception(f"Failed to publish task to Redis: {e}")
        return None


class TextServiceClient:
    """
    Enqueue a Celery task that the text-service worker consumes.
    """

    def __init__(self):
        pass

    def process_acquisition(self, source: str, entry_id: str) -> Optional[Dict[str, Any]]:
        """
        Enqueue async processing by file path and entry ID.

        Args:
            source: Relative file path from storage root
            entry_id: The acquisition/entry ID as a string for indexing
        """
        try:
            if not source or not entry_id:
                logger.error("Invalid parameters for text processing")
                return None

            task_id = _publish_task_to_redis(
                args=[source, str(entry_id)],
            )

            if task_id:
                return {"task_id": task_id, "source": source, "entry_id": entry_id}
            return None
        except Exception:
            logger.exception("Failed to enqueue text processing task")
            return None
