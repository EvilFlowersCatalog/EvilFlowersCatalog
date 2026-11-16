from django.conf import settings
from django.test import SimpleTestCase

from evil_flowers_catalog.celery import app


class MaintenanceQueueRoutingTest(SimpleTestCase):
    """#193: long maintenance tasks leave the autoscaled general worker's queue."""

    def _queue(self, task: str):
        return app.amqp.router.route({}, task)["queue"]

    def test_maintenance_tasks_go_to_their_own_queue(self):
        for task in settings.EVILFLOWERS_MAINTENANCE_TASKS:
            with self.subTest(task=task):
                queue = self._queue(task)
                self.assertEqual(queue.name, settings.EVILFLOWERS_MAINTENANCE_QUEUE)
                self.assertEqual(queue.exchange.name, settings.EVILFLOWERS_MAINTENANCE_QUEUE)
                self.assertEqual(queue.exchange.type, "direct")
                self.assertEqual(queue.routing_key, settings.EVILFLOWERS_MAINTENANCE_QUEUE)

    def test_short_tasks_stay_on_the_default_queue(self):
        for task in ("apps.tasks.tasks.reconcile_jobs", "apps.readium.tasks.sweep_unclaimed_reservations"):
            with self.subTest(task=task):
                self.assertEqual(self._queue(task).name, app.conf.task_default_queue)

    def test_backup_acks_on_receipt(self):
        from apps.tasks.tasks import backup

        self.assertFalse(backup.acks_late)
        self.assertFalse(backup.reject_on_worker_lost)
