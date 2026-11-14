from django.apps import AppConfig


class TasksConfig(AppConfig):
    name = "apps.tasks"

    def ready(self):
        from celery.signals import before_task_publish

        from apps.tasks import ledger
        from apps.tasks.kinds import registry

        registry.load()

        # IP-031: every task this process sends is in the ledger before a worker can take it,
        # whoever sends it (JobService, a `.delay()` in a signal, beat).
        def _record(sender=None, body=None, headers=None, routing_key=None, **kwargs):
            if headers:
                ledger.record_publish(headers, body, routing_key)

        before_task_publish.connect(_record, weak=False, dispatch_uid="apps.tasks.record_publish")
