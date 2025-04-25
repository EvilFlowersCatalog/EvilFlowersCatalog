from django.conf import settings
from django.utils.module_loading import import_string

from .executors.executor import Executor
from .executors.celery_executor import CeleryExecurtor
from .executors.kafka_executor import KafkaExecutor
from .transformers import Transformer


class EventExecutionService:
    executor: Executor
    transformer: Transformer

    def __init__(self):
        self.transformer = import_string(settings.EVILFLOWERS_EVENT_BROKER_TRANSFORMER)()

    def execute(self, event: str, payload: dict):
        transformer_payload = self.transformer.transform(payload)
        self.executor.send(event, transformer_payload)


class KafkaEventExecutionService(EventExecutionService):
    def __init__(self):
        super().__init__()
        self.executor = KafkaExecutor()


class CeleryEventExecutionService(EventExecutionService):
    def __init__(self):
        super().__init__()
        self.executor = CeleryExecurtor()


def get_event_broker() -> EventExecutionService:
    return import_string(settings.EVILFLOWERS_EVENT_BROKER_EXECUTOR)()
