"""IP-031 rev. 2: the job and run state machines, one test per arrow of `state-job` / `state-job-run`."""

import subprocess
import sys
from unittest import TestCase

from apps.tasks.domain import machine
from apps.tasks.domain.machine import Action
from apps.tasks.domain.values import (
    Blocked,
    DesiredState as D,
    JobSnapshot,
    JobState as S,
    Ready,
    Refused,
    RunStatus as R,
)


def job(state=S.PENDING, desired=D.COMPLETED, generation=1, observed=0, attempts=0, max_attempts=1, run=None):
    return JobSnapshot(
        state=state,
        desired=desired,
        generation=generation,
        observed_generation=observed,
        attempts=attempts,
        max_attempts=max_attempts,
        run_status=run,
    )


class ReconcileTests(TestCase):
    def test_pending_behind_is_checked(self):
        self.assertIs(machine.reconcile(job()), Action.CHECK)

    def test_pending_with_a_retry_due_is_checked(self):
        self.assertIs(machine.reconcile(job(observed=1, attempts=1, max_attempts=3)), Action.CHECK)

    def test_pending_without_anything_to_do_waits(self):
        self.assertIs(machine.reconcile(job(observed=1, attempts=1)), Action.NOTHING)

    def test_blocked_is_rechecked(self):
        self.assertIs(machine.reconcile(job(S.BLOCKED, observed=1)), Action.CHECK)

    def test_active_jobs_wait_for_their_run(self):
        for state in (S.QUEUED, S.RUNNING):
            self.assertIs(machine.reconcile(job(state, observed=0)), Action.NOTHING)

    def test_discovered_settled_jobs_never_dispatch(self):
        for state in (S.SUCCEEDED, S.FAILED):
            self.assertIs(machine.reconcile(job(state, generation=0, observed=0)), Action.NOTHING)

    def test_cancel_waiting(self):
        for state in (S.PENDING, S.BLOCKED):
            self.assertIs(machine.reconcile(job(state, D.CANCELLED)), Action.CANCEL)

    def test_cancel_queued_revokes(self):
        self.assertIs(machine.reconcile(job(S.QUEUED, D.CANCELLED)), Action.REVOKE)

    def test_cancel_running_stops(self):
        self.assertIs(machine.reconcile(job(S.RUNNING, D.CANCELLED)), Action.STOP)

    def test_cancelling_ends_when_the_run_ended(self):
        self.assertIs(machine.reconcile(job(S.CANCELLING, D.CANCELLED, run=R.STARTED)), Action.NOTHING)
        self.assertIs(machine.reconcile(job(S.CANCELLING, D.CANCELLED, run=R.SUCCEEDED)), Action.CANCEL)

    def test_check_outcomes(self):
        self.assertIs(machine.after_check(Ready()), S.QUEUED)
        self.assertIs(machine.after_check(Blocked("loans")), S.BLOCKED)
        self.assertIs(machine.after_check(Refused("not a PDF")), S.FAILED)


class ApplyTests(TestCase):
    def test_new_job(self):
        self.assertEqual(
            machine.apply(None, desired=D.COMPLETED, spec_changed=True, stale=False),
            machine.ApplyOutcome(True, S.PENDING),
        )

    def test_same_spec_on_an_active_job_is_a_no_op(self):
        outcome = machine.apply(job(S.QUEUED, observed=1), desired=D.COMPLETED, spec_changed=False, stale=False)
        self.assertEqual(outcome, machine.ApplyOutcome(False, S.QUEUED))

    def test_same_spec_on_a_stale_job_runs_again(self):
        outcome = machine.apply(job(S.QUEUED, observed=1), desired=D.COMPLETED, spec_changed=False, stale=True)
        self.assertTrue(outcome.new_generation)

    def test_apply_on_a_settled_job_is_a_retry(self):
        for state in (S.SUCCEEDED, S.FAILED, S.CANCELLED):
            outcome = machine.apply(job(state, observed=1), desired=D.COMPLETED, spec_changed=False, stale=False)
            self.assertEqual(outcome, machine.ApplyOutcome(True, S.PENDING))

    def test_changed_spec_while_running_waits_for_the_run(self):
        outcome = machine.apply(job(S.RUNNING, observed=1), desired=D.COMPLETED, spec_changed=True, stale=False)
        self.assertEqual(outcome, machine.ApplyOutcome(True, S.RUNNING))

    def test_cancel_is_left_to_the_reconciler(self):
        outcome = machine.apply(job(S.RUNNING, observed=1), desired=D.CANCELLED, spec_changed=False, stale=False)
        self.assertEqual(outcome, machine.ApplyOutcome(False, S.RUNNING))


class RunMovesJobTests(TestCase):
    def test_run_statuses_map_to_job_states(self):
        cases = {
            R.QUEUED: S.QUEUED,
            R.RECEIVED: S.QUEUED,
            R.STARTED: S.RUNNING,
            R.RETRYING: S.RUNNING,
            R.SUCCEEDED: S.SUCCEEDED,
            R.REVOKED: S.CANCELLED,
            R.FAILED: S.FAILED,
            R.LOST: S.FAILED,
            R.REJECTED: S.FAILED,
        }
        for run, state in cases.items():
            self.assertIs(machine.on_run(job(S.RUNNING, observed=1, attempts=1), run), state, run)

    def test_failure_with_attempts_left_goes_back_to_pending(self):
        self.assertIs(machine.on_run(job(S.RUNNING, observed=1, attempts=1, max_attempts=3), R.FAILED), S.PENDING)

    def test_a_new_generation_applied_meanwhile_is_next(self):
        self.assertIs(machine.on_run(job(S.RUNNING, generation=2, observed=1, attempts=1), R.SUCCEEDED), S.PENDING)

    def test_cancelling_follows_the_run(self):
        self.assertIs(machine.on_run(job(S.CANCELLING, D.CANCELLED), R.STARTED), S.CANCELLING)
        self.assertIs(machine.on_run(job(S.CANCELLING, D.CANCELLED), R.SUCCEEDED), S.CANCELLED)

    def test_domain_holds_back_success_but_never_masks_failure(self):
        self.assertIs(machine.settle_with_observation(S.SUCCEEDED, S.RUNNING), S.RUNNING)
        self.assertIs(machine.settle_with_observation(S.FAILED, S.SUCCEEDED), S.FAILED)
        self.assertIs(machine.settle_with_observation(S.RUNNING, S.FAILED), S.RUNNING)
        self.assertIs(machine.settle_with_observation(S.CANCELLED, S.SUCCEEDED), S.CANCELLED)
        self.assertIs(machine.settle_with_observation(S.SUCCEEDED, None), S.SUCCEEDED)


class RunPrecedenceTests(TestCase):
    def test_first_fact_is_accepted(self):
        self.assertTrue(machine.run_accepts(None, R.STARTED, newer=True))

    def test_older_facts_are_history(self):
        self.assertFalse(machine.run_accepts(R.SUCCEEDED, R.STARTED, newer=False))

    def test_revoked_is_final(self):
        self.assertFalse(machine.run_accepts(R.REVOKED, R.RECEIVED, newer=True))

    def test_finished_runs_reopen_only_on_received_or_started(self):
        self.assertTrue(machine.run_accepts(R.FAILED, R.RECEIVED, newer=True))
        self.assertTrue(machine.run_accepts(R.LOST, R.STARTED, newer=True))
        self.assertFalse(machine.run_accepts(R.SUCCEEDED, R.FAILED, newer=True))

    def test_late_sent_never_moves_a_run_back(self):
        self.assertFalse(machine.run_accepts(R.RECEIVED, R.QUEUED, newer=True))

    def test_retry_only_from_a_delivered_run(self):
        self.assertTrue(machine.run_accepts(R.STARTED, R.RETRYING, newer=True))
        self.assertFalse(machine.run_accepts(R.QUEUED, R.RETRYING, newer=True))


class LayeringTests(TestCase):
    def test_the_domain_imports_neither_django_nor_celery(self):
        code = (
            "import sys, apps.tasks.domain, apps.tasks.domain.machine; "
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('django', 'celery', 'kombu')); "
            "print(','.join(bad)); sys.exit(1 if bad else 0)"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
