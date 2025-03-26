from django.conf import settings
from .executors.celery_executor import CeleryExecurtor
from .executors.kafka_executor import KafkaExecutor
from .transformers.celery_transformer import CeleryTransformer
from .transformers.kafka_transformer import KafkaTransformer


class EventExecutionService:
    def __init__(self):
        self.broker = getattr(settings, "EVILFLOWERS_EVENT_BROKER")
        self.executor = self.select_backend()
        self.transformer = self.select_transformer()

    def select_backend(self):
        if self.broker == "celery":
            return CeleryExecurtor()
        elif self.broker == "kafka":
            return KafkaExecutor()
        raise ValueError("Invalid broker name, choose 'celery' or 'kafka'.")

    def select_transformer(self):
        if self.broker == "celery":
            return CeleryTransformer()
        elif self.broker == "kafka":
            return KafkaTransformer()
        raise ValueError("Invalid broker name, choose 'celery' or 'kafka'.")

    def execute(self, event: str, payload: dict):
        transformer_payload = self.transformer.transform(payload)
        self.executor.send(event, transformer_payload)


event_servise = EventExecutionService()
