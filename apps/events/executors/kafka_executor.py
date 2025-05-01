import json
from kafka import KafkaProducer
from django.conf import settings
from django.db.models import Model
from .executor import Executor


class KafkaExecutor(Executor):
    def __init__(self):
        self.producer = KafkaProducer(
            bootstrap_servers=getattr(settings, "KAFKA_BROKER_URL"),
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )

    def send(self, event: str, payload: dict | str):
        self.producer.send(event, payload)
        self.producer.flush()
