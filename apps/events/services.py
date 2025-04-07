from django.conf import settings
from django.utils.module_loading import import_string

from .executors.executor import Executor
from .executors.celery_executor import CeleryExecurtor
from .executors.kafka_executor import KafkaExecutor
from .transformers.transformer import Transformer
from .transformers.celery_transformer import CeleryTransformer
from .transformers.kafka_transformer import KafkaTransformer


class EventExecutionService:
    executor: Executor
    transformer: Transformer

    def execute(self, event: str, payload: dict):
        transformer_payload = self.transformer.transform(payload)
        self.executor.send(event, transformer_payload)


class KafkaEventExecutionService(EventExecutionService):
    def __init__(self):
        self.executor = KafkaExecutor()
        self.transformer = KafkaTransformer()


class CeleryEventExecutionService(EventExecutionService):
    def __init__(self):
        self.executor = CeleryExecurtor()
        self.transformer = CeleryTransformer()


def get_event_broker() -> EventExecutionService:
    return import_string(settings.EVILFLOWERS_EVENT_BROKER)()
