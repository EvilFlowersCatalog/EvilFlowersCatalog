"""#154: the off-peak sweeps declare section summaries and figure descriptions; the graph is rebuilt nightly."""

from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import Retry
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.assistant import tasks
from apps.assistant.sections import SUMMARISE_KIND, next_to_summarise
from apps.assistant.tests import fixtures
from apps.core.models import Entry
from apps.tasks.application.facade import facade
from apps.tasks.application.registry import registry
from apps.tasks.domain.values import DesiredState, JobState
from apps.tasks.models import Job
from apps.tasks.tests.test_jobs import FakeDispatcher

CHAT_ACTIVE = "apps.assistant.services.chat_active"


def report(**counts):
    return {"sections": 2, "summarised": 0, "kept": 0, "skipped": 0, "failed": 0, "paused": 0, **counts}


class Sweeps(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.catalog = fixtures.catalog(cls.admin)
        cls.popular, cls.popular_run, *_ = fixtures.publication(cls.admin, cls.catalog, title="Popular")
        cls.quiet, *_ = fixtures.publication(cls.admin, cls.catalog, title="Quiet")
        cls.locked, *_ = fixtures.publication(cls.admin, cls.catalog, title="Locked", readium=True)
        Entry.objects.filter(pk=cls.popular.pk).update(popularity=50)
        Entry.objects.filter(pk=cls.locked.pk).update(popularity=100)

    def setUp(self):
        self.dispatcher = FakeDispatcher()
        patcher = patch.object(facade, "dispatcher", self.dispatcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settle(self, entry, state, at=None, desired=DesiredState.COMPLETED):
        Job.objects.filter(kind=SUMMARISE_KIND, entry=entry).update(
            state=state, desired_state=desired, last_transition_at=at or timezone.now()
        )

    def declare(self, entry):
        from apps.tasks.contract import declare

        return declare(SUMMARISE_KIND, entry.pk)

    # -- which publications are next -------------------------------------------------------------------

    def test_most_read_first_never_lcp_and_never_unprocessed(self):
        fixtures_entry = Entry.objects.create(creator=self.admin, catalog=self.catalog, title="No text", popularity=99)
        self.assertEqual(next_to_summarise(10, 20), [self.popular.pk, self.quiet.pk])
        self.assertNotIn(fixtures_entry.pk, next_to_summarise(10, 20))
        self.assertEqual(next_to_summarise(0, 20), [])

    def test_a_job_on_its_way_or_cancelled_keeps_its_entry_out(self):
        self.declare(self.popular)  # queued
        self.assertEqual(next_to_summarise(10, 20), [self.quiet.pk])
        self.settle(self.popular, JobState.CANCELLED, desired=DesiredState.CANCELLED)
        self.assertEqual(next_to_summarise(10, 20), [self.quiet.pk])

    def test_succeeded_is_done_until_the_text_is_extracted_again(self):
        self.declare(self.popular)
        self.settle(self.popular, JobState.SUCCEEDED)
        self.assertEqual(next_to_summarise(10, 20), [self.quiet.pk])
        # A newer extraction run became active after the job succeeded: due again.
        type(self.popular_run).objects.filter(pk=self.popular_run.pk).update(
            finished_at=timezone.now() + timedelta(minutes=5)
        )
        self.assertEqual(next_to_summarise(10, 20), [self.popular.pk, self.quiet.pk])

    def test_a_failed_book_waits_out_its_cooldown(self):
        self.declare(self.popular)
        self.settle(self.popular, JobState.FAILED)
        self.assertEqual(next_to_summarise(10, 20), [self.quiet.pk])
        self.settle(self.popular, JobState.FAILED, at=timezone.now() - timedelta(hours=21))
        self.assertEqual(next_to_summarise(10, 20), [self.popular.pk, self.quiet.pk])

    # -- the sweep tick ---------------------------------------------------------------------------------

    @override_settings(EVILFLOWERS_ASSISTANT_SUMMARISE_CONCURRENCY=1)
    def test_each_tick_tops_up_to_the_concurrency_and_moves_on(self):
        with patch(CHAT_ACTIVE, return_value=False):
            first = tasks.summarise_sweep()
            self.assertEqual(first["entries"], [str(self.popular.pk)])
            self.assertEqual(tasks.summarise_sweep()["declared"], 0)  # still on its way
            self.settle(self.popular, JobState.SUCCEEDED)
            self.assertEqual(tasks.summarise_sweep()["entries"], [str(self.quiet.pk)])
        sent = [(item["task"], item["args"]) for item in self.dispatcher.sent]
        self.assertEqual(
            sent,
            [
                ("apps.assistant.tasks.summarise_sections", [str(self.popular.pk), False]),
                ("apps.assistant.tasks.summarise_sections", [str(self.quiet.pk), False]),
            ],
        )

    def test_no_tick_while_a_reader_chats(self):
        with patch(CHAT_ACTIVE, return_value=True):
            self.assertEqual(tasks.summarise_sweep(), {"declared": 0, "reason": "chat"})
        self.assertEqual(self.dispatcher.sent, [])

    def test_figure_ticks_never_stack_runs(self):
        with override_settings(EVILFLOWERS_ASSISTANT_DESCRIBE_FIGURES_BATCH=7):
            tasks.describe_figures_sweep()
            tasks.describe_figures_sweep()
        (sent,) = self.dispatcher.sent
        self.assertEqual((sent["task"], sent["kwargs"]), ("apps.assistant.tasks.describe_figures", {"limit": 7}))

    # -- one publication's run --------------------------------------------------------------------------

    def test_a_paused_run_continues_in_a_fresh_one(self):
        with (
            patch("apps.assistant.sections.summarise_entry", return_value=report(summarised=1, paused=1)),
            patch(CHAT_ACTIVE, return_value=False),
            patch.object(tasks.summarise_sections, "retry", side_effect=Retry()) as retry,
        ):
            with self.assertRaises(Retry):
                tasks.summarise_sections(str(self.popular.pk))
        retry.assert_called_once_with(countdown=1)

    def test_pause_yields_to_chats_and_to_the_slice(self):
        seen = {}

        def capture(entry_id, force, pause):
            seen["pause"] = pause
            return report(summarised=2)

        with patch("apps.assistant.sections.summarise_entry", side_effect=capture):
            tasks.summarise_sections(str(self.popular.pk))
        with patch(CHAT_ACTIVE, return_value=True):
            self.assertTrue(seen["pause"]())
        with patch(CHAT_ACTIVE, return_value=False):
            self.assertFalse(seen["pause"]())
            with patch.object(tasks.time, "monotonic", return_value=float("inf")):
                self.assertTrue(seen["pause"]())

    def test_a_forced_run_never_pauses(self):
        with patch("apps.assistant.sections.summarise_entry", return_value=report(summarised=2)) as summarise:
            tasks.summarise_sections(str(self.popular.pk), True)
        self.assertIsNone(summarise.call_args.kwargs["pause"])

    def test_failed_sections_fail_the_run(self):
        with patch("apps.assistant.sections.summarise_entry", return_value=report(summarised=1, failed=1)):
            with self.assertRaisesRegex(RuntimeError, "1 of 2 sections"):
                tasks.summarise_sections(str(self.popular.pk))

    def test_summarise_entry_stops_when_asked(self):
        from apps.assistant.sections import summarise_entry

        result = summarise_entry(self.popular.pk, client=object(), pause=lambda: True)
        self.assertEqual((result["paused"], result["summarised"], result["failed"]), (1, 0, 0))


class GraphRebuildTests(TestCase):
    def test_skipped_where_age_is_missing(self):
        from apps.graph import tasks as graph_tasks

        with (
            patch("apps.graph.age.create_graph", return_value=False),
            patch("apps.graph.projection.rebuild") as rebuild,
        ):
            self.assertEqual(graph_tasks.rebuild_graph(), {"skipped": "age not installed"})
        rebuild.assert_not_called()

    def test_rebuilds_where_age_is_installed(self):
        from apps.graph import tasks as graph_tasks

        with (
            patch("apps.graph.age.create_graph", return_value=True),
            patch("apps.graph.projection.rebuild", return_value={"runs": 2, "sections": 9}),
        ):
            self.assertEqual(graph_tasks.rebuild_graph(), {"runs": 2, "sections": 9})


class KindsTests(TestCase):
    def test_the_sweeps_are_maintenance_kinds(self):
        for kind, task in (
            ("maintenance.assistant_summarise_sweep", "apps.assistant.tasks.summarise_sweep"),
            ("maintenance.assistant_describe_figures_sweep", "apps.assistant.tasks.describe_figures_sweep"),
            ("maintenance.graph_rebuild", "apps.graph.tasks.rebuild_graph"),
        ):
            definition = registry.get(kind)
            self.assertIsNotNone(definition, kind)
            self.assertEqual(definition.task, task)
        self.assertEqual(registry.get("maintenance.graph_rebuild").time_limit, timedelta(hours=2))
        self.assertEqual(registry.get(SUMMARISE_KIND).max_attempts, 3)
