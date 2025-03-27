from celery import signature, group


class CeleryExecurtor:
    def send(self, event: str, payload: dict):
        group(signature(event, payload)).apply_async()
