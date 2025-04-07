from .transformer import Transformer


class KafkaTransformer(Transformer):
    def transform(self, payload: dict) -> dict:

        return payload
