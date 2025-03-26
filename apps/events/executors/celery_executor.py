from ..celety_tasks import process_task


class CeleryExecurtor:
    def send(self, event: str, payload: dict):
        process_task.delay(event, payload)
