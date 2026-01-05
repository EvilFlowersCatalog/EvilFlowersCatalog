import logging
from typing import Optional, Dict, Any
import json as json_lib
import uuid

from django.conf import settings
from kombu import Connection, Exchange, Queue, Producer

logger = logging.getLogger(__name__)


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
        # Create exchange and queue matching Celery's defaults
        exchange = Exchange('celery', type='direct')
        queue = Queue(queue_name, exchange=exchange, routing_key=queue_name)
        
        # Generate task ID (Celery format)
        task_id = str(uuid.uuid4())
        
        # Create task message body (required fields)
        task_body = {
            'task': task_name,
            'id': task_id,
            'args': args,
            'kwargs': {},
        }
        
        # Create task message headers (required metadata)
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
        
        # Connect to Redis and publish with both body and headers
        # Serialize the body to JSON string first (kombu with Redis transport needs this)
        task_body_json = json_lib.dumps(task_body)
        
        with Connection(broker_url) as conn:
            # Ensure the channel is ready
            channel = conn.channel()
            producer = Producer(
                channel,
                exchange=exchange, 
                routing_key=queue_name,
                serializer='json'
            )
            producer.publish(
                task_body,  # Pass dict, serializer will handle it
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
        
        logger.debug(f"Published task to Redis: task_id={task_id}, task={task_name}, queue={queue_name}")
        return task_id
        
    except Exception as e:
        logger.exception(f"Failed to publish task to Redis: {e}")
        return None


class TextServiceClient:
    """
    Enqueue a Celery task that the text-service worker consumes.
    """

    def __init__(self):
        self.task_name = getattr(settings, "TEXT_SERVICE_TASK_NAME", "text_service.process_pdf")
        self.queue = getattr(settings, "TEXT_SERVICE_QUEUE", None)  # None = default queue

    def process_acquisition(self, acquisition_url: str) -> Optional[Dict[str, Any]]:
        """
        Enqueue async processing by acquisition download URL.
        
        Args:
            acquisition_url: Full URL where the acquisition file can be downloaded
        """
        try:
            # Validate URL format
            if not acquisition_url or not acquisition_url.startswith(('http://', 'https://')):
                logger.error(f"Invalid acquisition_url format (must be a full HTTP/HTTPS URL): {acquisition_url}")
                return None
            
            queue_name = self.queue if self.queue else "celery"
            
            # Use kombu directly to publish to Redis
            task_id = _publish_task_to_redis(
                self.task_name,
                args=[acquisition_url],
                queue_name=queue_name,
            )
            
            if task_id:
                logger.info(f"Enqueued text processing task: acquisition_url={acquisition_url}, task_id={task_id}, queue={queue_name}")
                return {"task_id": task_id, "acquisition_url": acquisition_url}
            else:
                logger.error(f"Failed to publish task for acquisition_url={acquisition_url}")
                return None
        except Exception as e:
            logger.exception(f"Failed to enqueue text processing task: {e}")
            return None


    def process_file(self, *args, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Backward-compatible shim.
        Expect caller to pass acquisition_url either as:
          process_file(acquisition_url="...") 
          OR process_file("http://...")
        """
        acquisition_url = kwargs.get("acquisition_url")
        
        if acquisition_url is None and args:
            acquisition_url = args[0] if len(args) > 0 else None

        if not acquisition_url:
            logger.warning("process_file called without required parameter (acquisition_url); skipping.")
            return None

        return self.process_acquisition(str(acquisition_url))
