from .transformer import Transformer


class CeleryTransformer(Transformer):
    def transform(self, payload: dict) -> dict:
        if "args" not in payload:
            payload["args"] = []
        if "kwargs" not in payload:
            payload["kwargs"] = {}
        if "queue" not in payload:
            payload["queue"] = ""
        return payload
