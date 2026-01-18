import logging
from typing import Optional, Dict, Any
import uuid

from django.conf import settings
from kombu import Connection, Exchange, Queue, Producer

logger = logging.getLogger('apps.api.services.text_service_client')


def _get_broker_url() -> str:
    """
    Django must point to the SAME Redis broker that the text-service worker uses.
    Configure in settings / env:
      TEXT_SERVICE_REDIS_URL=redis://...
    """
    broker_url = getattr(settings, "TEXT_SERVICE_REDIS_URL", "redis://redis:6379/0")
    return broker_url


def _publish_task_to_redis(task_name: str, args: list, queue_name: str = 'celery') -> Optional[str]:
    """
    Publish a Celery task directly to Redis using kombu.
    Returns task_id if successful, None otherwise.
    """
    broker_url = _get_broker_url()
    
    try:
        exchange = Exchange(queue_name, type='direct')
        queue = Queue(queue_name, exchange=exchange, routing_key=queue_name)
        task_id = str(uuid.uuid4())
        
        task_body = {
            'task': task_name,
            'id': task_id,
            'args': args,
            'kwargs': {},
        }
        
        task_headers = {
            'lang': 'py',
            'task': task_name,
            'id': task_id,
            'root_id': task_id,
            'parent_id': None,
            'group': None,
            'retries': 0,
            'eta': None,
            'expires': None,
            'utc': True,
        }
        
        with Connection(broker_url) as conn:
            channel = conn.channel()
            producer = Producer(
                channel,
                exchange=exchange, 
                routing_key=queue_name,
                serializer='json'
            )
            
            producer.publish(
                task_body,
                headers=task_headers,
                exchange=exchange.name,
                routing_key=queue_name,
                retry=True,
                retry_policy={
                    'max_retries': 3,
                    'interval_start': 0,
                    'interval_step': 0.2,
                    'interval_max': 0.2,
                }
            )
        
        return task_id
        
    except Exception:
        logger.exception("Failed to publish task to Redis")
        return None


class TextServiceClient:
    """
    Enqueue a Celery task that the text-service worker consumes.
    """

    def __init__(self):
        self.task_name = getattr(settings, "TEXT_SERVICE_TASK_NAME", "evilflowers_text_worker.process_pdf")
        self.queue = getattr(settings, "TEXT_SERVICE_QUEUE", "evilflowers_text_worker")

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
            
            queue_name = self.queue if self.queue else "evilflowers_text_worker"
            task_id = _publish_task_to_redis(
                self.task_name,
                args=[source, str(entry_id)],
                queue_name=queue_name,
            )
            
            if task_id:
                return {"task_id": task_id, "source": source, "entry_id": entry_id}
            return None
        except Exception:
            logger.exception("Failed to enqueue text processing task")
            return None
