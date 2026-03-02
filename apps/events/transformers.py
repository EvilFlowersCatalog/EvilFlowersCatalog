from django.core import serializers
from django.db.models import Model


class Transformer:
    def transform(self, payload: dict) -> dict:
        pass


class DjangoTransformer(Transformer):
    def transform(self, payload: dict | Model) -> dict | Model:
        if isinstance(payload, Model):
            payload = serializers.serialize("json", [payload])
            # deserialization: `list(serializers.deserialize("json", data))[0].object`
        elif isinstance(payload, dict):
            pass
        else:
            raise TypeError("Unsupported type of payload")

        return payload
