from django.apps import AppConfig


class TasksConfig(AppConfig):
    name = "apps.tasks"

    def ready(self):
        from celery.signals import before_task_publish
        from django.db import transaction
        from django.db.models.signals import post_save

        from apps.tasks.application.registry import registry
        from apps.tasks.infrastructure import ledger

        registry.load()

        # IP-031: every task this process sends is a run in the ledger before a worker can take it,
        # whoever sends it (the facade, a `.delay()` in a signal, beat).
        def _record(sender=None, body=None, headers=None, routing_key=None, **kwargs):
            if headers:
                ledger.record_publish(headers, body, routing_key)

        before_task_publish.connect(_record, weak=False, dispatch_uid="apps.tasks.record_publish")

        # Domain changes a definition observes keep its jobs in step (e.g. EncryptedContent → readium.encrypt).
        for definition in registry.all():
            for model in definition.observes:

                def _observe(sender, instance, definition=definition, **kwargs):
                    subject = definition.subject_of(instance)
                    if subject is None:
                        return

                    def run():
                        from apps.tasks.application.facade import facade

                        try:
                            facade.observe(definition.kind, subject)
                        except Exception:  # noqa: BLE001 — an observer must never fail the domain write
                            import logging

                            logging.getLogger(__name__).exception("jobs.observe_failed kind=%s", definition.kind)

                    transaction.on_commit(run)

                post_save.connect(
                    _observe, sender=model, weak=False, dispatch_uid=f"apps.tasks.observe.{definition.kind}.{model}"
                )
