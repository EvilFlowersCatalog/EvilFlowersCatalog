"""The Celery event listener survives events it cannot store (#108)."""

import uuid

from django.test import SimpleTestCase, TestCase

from apps.extraction import services
from apps.tasks.management.commands.celery_event_handler import Command
from apps.tasks.models import JobProtocol

LEGACY_EXTRACT_ID = "extract:f89dbe0d-36c2-45ee-b3b4-931598f26634:v2"


def event(type_="task-received", **fields):
    return {"type": type_, "name": "evilflowers_analyzer.extract", **fields}


class EventHandlerTests(TestCase):
    def setUp(self):
        self.command = Command()

    def test_non_uuid_task_id_is_skipped_and_listener_keeps_going(self):
        with self.assertLogs("apps.tasks.management.commands.celery_event_handler", "ERROR") as logs:
            self.command.handle_event(event(uuid=LEGACY_EXTRACT_ID))
        self.assertIn(LEGACY_EXTRACT_ID, logs.output[0])

        task_id = str(uuid.uuid4())
        self.command.handle_event(event(uuid=task_id))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, JobProtocol.JobStatus.RECEIVED)

    def test_non_uuid_parent_id_is_skipped_and_listener_keeps_going(self):
        child = str(uuid.uuid4())
        with self.assertLogs("apps.tasks.management.commands.celery_event_handler", "ERROR") as logs:
            self.command.handle_event(event(uuid=child, parent_id=LEGACY_EXTRACT_ID))
        self.assertIn(LEGACY_EXTRACT_ID, logs.output[0])
        self.assertFalse(JobProtocol.objects.filter(pk=child).exists())

        task_id = str(uuid.uuid4())
        self.command.handle_event(event("task-started", uuid=task_id, hostname="worker@1"))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).worker, "worker@1")

    def test_args_holding_a_set_are_stored(self):
        task_id = str(uuid.uuid4())
        self.command.handle_event(event(uuid=task_id, args="({'b', 'a'}, b'x', (1, 2))", kwargs="{'ids': {3}, 1: ()}"))
        protocol = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(protocol.job_args, [["a", "b"], "b'x'", [1, 2]])
        self.assertEqual(protocol.job_kwargs, {"ids": [3], "1": []})

    def test_extraction_task_and_its_follow_up_are_recorded(self):
        parent = services.task_id(uuid.uuid4(), 2)
        child = str(uuid.uuid4())
        self.command.handle_event(event(uuid=parent))
        self.command.handle_event(event(uuid=child, parent_id=parent))
        self.assertEqual(JobProtocol.objects.get(pk=child).parent_id, uuid.UUID(parent))


class ExtractionTaskIdTests(SimpleTestCase):
    def test_task_id_is_a_deterministic_uuid_per_acquisition_and_version(self):
        acquisition_id = uuid.uuid4()
        task_id = services.task_id(acquisition_id, 2)
        self.assertEqual(str(uuid.UUID(task_id)), task_id)
        self.assertEqual(services.task_id(str(acquisition_id), 2), task_id)
        self.assertNotEqual(services.task_id(acquisition_id, 3), task_id)
        self.assertNotEqual(services.task_id(uuid.uuid4(), 2), task_id)
