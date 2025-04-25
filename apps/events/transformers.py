class Transformer:
    def transform(self, payload: dict) -> dict:
        pass


class DjangoTransformer(Transformer):
    def transform(self, payload: dict) -> dict:
        if "args" not in payload:
            payload["args"] = []
        if "kwargs" not in payload:
            payload["kwargs"] = {}
        if "queue" not in payload:
            payload["queue"] = ""
        # TODO: serialize objects with django serializer
        return payload
