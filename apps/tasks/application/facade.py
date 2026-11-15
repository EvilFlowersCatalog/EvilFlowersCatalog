"""JobFacade (IP-031 rev. 2): the one way into background jobs.

Clients declare (`apply`, `patch`); facts arrive (`run_published`, `run_changed`, `observe`,
`report`); the reconciler (`reconcile`) compares desired and observed state and carries out the one
transition the job state machine (`apps.tasks.domain.machine`) allows. Sending happens after the
job's transaction commits, so a worker never sees a run the ledger does not have.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from pydantic import ValidationError

from apps.core.services.capability_tokens import CapabilityTokenService
from apps.tasks.application.registry import Registry, registry as default_registry
from apps.tasks.capability_scopes import JOB_RUN_OUTCOME
from apps.tasks.domain import machine
from apps.tasks.domain.contract import Dispatch, DispatchError, Dispatcher, JobDefinition, Report, Spec
from apps.tasks.domain.machine import Action
from apps.tasks.domain.values import (
    Blocked,
    Check,
    DesiredState,
    JobSnapshot,
    JobState,
    Ready,
    Refused,
    RunStatus,
)
from apps.tasks.infrastructure.dispatcher import CeleryDispatcher, running_as
from apps.tasks.models import Job, JobEvent, JobRun

logger = logging.getLogger("apps.tasks.audit")


def is_stale(job: Job, now=None) -> bool:
    """Queued for longer than EVILFLOWERS_JOBS_STALE_MINUTES: the run almost certainly never reached a worker."""
    if job.state != JobState.QUEUED or job.last_transition_at is None:
        return False
    now = now or timezone.now()
    return now - job.last_transition_at > timedelta(minutes=settings.EVILFLOWERS_JOBS_STALE_MINUTES)


def json_safe(value):
    from apps.tasks.infrastructure.ledger import json_safe as safe

    return safe(value)


@dataclass(frozen=True, slots=True)
class Plan:
    """A dry run: what an apply would do, nothing stored or sent."""

    kind: str
    subject_id: Any
    spec: Mapping[str, Any]
    check: Check
    preview: Mapping[str, Any] = field(default_factory=dict)
    job: Job | None = None

    @property
    def outcome(self) -> str:
        match self.check:
            case Ready():
                return "would-run"
            case Blocked():
                return "would-block"
            case Refused():
                return "refused"

    @property
    def reason(self) -> str | None:
        return getattr(self.check, "reason", None)


class JobFacade:
    def __init__(self, registry: Registry | None = None, dispatcher: Dispatcher | None = None):
        self.registry = registry or default_registry
        self.dispatcher = dispatcher or CeleryDispatcher()

    # -- queries --------------------------------------------------------------------------------------

    def jobs(self):
        return Job.objects.select_related("current_run")

    def get(self, job_id) -> Job:
        try:
            return self.jobs().get(pk=job_id)
        except (Job.DoesNotExist, ValueError, TypeError) as error:
            raise DispatchError(_("Job not found."), status=404) from error

    @staticmethod
    def summary(queryset) -> list[dict]:
        """Counts per kind and state, plus stale, for the dashboard's attention badges."""
        from django.db.models import Count

        cutoff = timezone.now() - timedelta(minutes=settings.EVILFLOWERS_JOBS_STALE_MINUTES)
        result: dict[str, dict] = {}
        for row in queryset.values("kind", "state").annotate(count=Count("id")).order_by("kind", "state"):
            result.setdefault(row["kind"], {"kind": row["kind"], "states": {}, "stale": 0})["states"][row["state"]] = (
                row["count"]
            )
        stale = queryset.filter(state=JobState.QUEUED, last_transition_at__lt=cutoff)
        for row in stale.values("kind").annotate(count=Count("id")):
            result.setdefault(row["kind"], {"kind": row["kind"], "states": {}, "stale": 0})["stale"] = row["count"]
        return list(result.values())

    # -- permissions ----------------------------------------------------------------------------------

    @staticmethod
    def require(user, definition: JobDefinition | None = None) -> None:
        permission = definition.permission if definition is not None else "core.change_entry"
        if user is None or not user.is_authenticated or not user.has_perm(permission):
            raise DispatchError(_("Background jobs are restricted to administrators."), status=403)

    # -- commands: declare ----------------------------------------------------------------------------

    def apply(
        self,
        kind: str,
        subject_id: Any = None,
        *,
        spec: Mapping | None = None,
        desired: DesiredState | str = DesiredState.COMPLETED,
        user=None,
        cause_id: Any = None,
        check_permission: bool = True,
        dry_run: bool = False,
    ) -> Job | Plan:
        """Declare that a job of `kind` on `subject_id` should be done with `spec` (or cancelled)."""
        definition = self.definition(kind)
        if check_permission:
            self.require(user, definition)
        if not definition.keyed:
            raise DispatchError(
                _("Jobs of kind '%(kind)s' are run-only: they are recorded, not declared.") % {"kind": kind},
                status=409,
            )
        desired = DesiredState(desired)
        subject = self.subject(definition, subject_id)
        spec_obj = self.parse_spec(definition, spec or {})
        check = definition.check(subject, spec_obj)
        if isinstance(check, Refused) and desired is DesiredState.COMPLETED:
            raise DispatchError(check.reason, status=422)
        existing = self._find(definition, subject)
        if dry_run:
            return Plan(
                kind,
                subject_id,
                spec_obj.model_dump(mode="json"),
                check,
                definition.preview(subject, spec_obj),
                existing,
            )

        spec_json = spec_obj.model_dump(mode="json")
        with transaction.atomic():
            job = self._lock_or_create(definition, subject)
            snapshot = None if self._is_new(job) else self.snapshot(job, definition)
            outcome = machine.apply(
                snapshot,
                desired=desired,
                spec_changed=snapshot is None or job.spec != spec_json,
                stale=snapshot is not None and is_stale(job),
            )
            job.desired_state = desired
            job.spec = spec_json
            if outcome.new_generation:
                job.generation += 1
                job.attempts = 0
                job.reason = None
                job.requested_by = user if getattr(user, "is_authenticated", False) else None
                job.cause_id = cause_id
            self._transition(
                job,
                outcome.state,
                "applied",
                generation=job.generation,
                desired=desired.value,
                user=str(user.pk) if getattr(user, "is_authenticated", False) else None,
            )
            job.save()
        # Reconciled at once, so the caller sees the outcome (queued, blocked …). A caller inside a larger
        # transaction that must not send before it commits declares from `transaction.on_commit`.
        job = self.reconcile(job.pk) or job
        logger.info(
            "jobs.apply kind=%s job=%s subject=%s generation=%s desired=%s new=%s",
            kind,
            job.pk,
            job.subject_id,
            job.generation,
            desired,
            outcome.new_generation,
        )
        return job

    def patch(
        self, job_id, *, spec: Mapping | None = None, desired: DesiredState | str | None = None, user=None
    ) -> Job:
        """Change a job's spec or desired state; same rules as apply."""
        job = self.get(job_id)
        return self.apply(
            job.kind,
            job.subject_id,
            spec=job.spec if spec is None else spec,
            desired=job.desired_state if desired is None else desired,
            user=user,
        )

    # -- the reconciler -------------------------------------------------------------------------------

    def reconcile(self, job_id) -> Job | None:
        """Take the one transition the state machine allows for this job; send after commit."""
        sends = []
        with transaction.atomic():
            job = Job.objects.select_for_update(of=("self",)).select_related("current_run").filter(pk=job_id).first()
            if job is None:
                return None
            definition = self.registry.get(job.kind)
            if definition is None:
                return job
            action = machine.reconcile(self.snapshot(job, definition))
            match action:
                case Action.NOTHING:
                    return job
                case Action.CANCEL:
                    self._transition(job, JobState.CANCELLED, "cancelled")
                case Action.REVOKE:
                    run = job.current_run
                    if run is not None and run.is_active:
                        run.status = RunStatus.REVOKED
                        run.finished_at = timezone.now()
                        run.error = run.error or "revoked"
                        run.save(update_fields=["status", "finished_at", "error", "updated_at"])
                        if not definition.stable_task_id:
                            # Workers remember revoked ids; a stable id would be refused on its next run.
                            sends.append(lambda run_id=str(run.pk): self.dispatcher.revoke(run_id))
                    self._transition(job, JobState.CANCELLED, "cancelled", revoked=run is not None)
                case Action.STOP:
                    run = job.current_run
                    if definition.terminate_safe and run is not None and not definition.stable_task_id:
                        sends.append(lambda run_id=str(run.pk): self.dispatcher.revoke(run_id, terminate=True))
                    self._transition(job, JobState.CANCELLING, "cancelling", terminate=definition.terminate_safe)
                case Action.CHECK:
                    sends.extend(self._check_and_queue(job, definition))
            job.save()
        for send in sends:
            send()
        if sends:
            job.refresh_from_db()  # a failed send moved the job on (failed / pending)
        return job

    def reconcile_due(self, limit: int = 500) -> int:
        """Reconcile every job that has something to do (beat, every minute)."""
        from django.db.models import Q

        due = (
            Job.objects.filter(
                Q(desired_state=DesiredState.COMPLETED, state__in=(JobState.PENDING, JobState.BLOCKED))
                | Q(
                    desired_state=DesiredState.CANCELLED,
                    state__in=(
                        JobState.PENDING,
                        JobState.BLOCKED,
                        JobState.QUEUED,
                        JobState.RUNNING,
                        JobState.CANCELLING,
                    ),
                )
            )
            .order_by("last_transition_at")
            .values_list("pk", flat=True)[:limit]
        )
        count = 0
        for job_id in list(due):
            try:
                self.reconcile(job_id)
                count += 1
            except Exception:
                logger.exception("jobs.reconcile_failed job=%s", job_id)
        return count

    def _check_and_queue(self, job: Job, definition: JobDefinition) -> list:
        subject = self._load_subject(definition, job.subject_id)
        if definition.subject_model is not None and subject is None:
            job.reason = _("The job's subject no longer exists.")
            self._transition(job, JobState.FAILED, "failed", reason=job.reason)
            return []
        try:
            spec = definition.spec_model.model_validate(job.spec)
        except ValidationError as error:
            job.reason = str(error)
            self._transition(job, JobState.FAILED, "failed", reason=job.reason)
            return []
        check = definition.check(subject, spec)
        state = machine.after_check(check)
        if state is not JobState.QUEUED:
            job.reason = check.reason
            self._transition(job, state, state.value, reason=check.reason)
            return []

        run = self._new_run(job, definition, subject, spec)
        job.current_run = run
        job.observed_generation = job.generation
        job.attempts += 1
        job.reason = None
        self._transition(job, JobState.QUEUED, "dispatched", run=str(run.pk), attempt=job.attempts)
        return [lambda: self._dispatch(definition, job.pk, run.pk, subject, spec)]

    def _new_run(self, job: Job, definition: JobDefinition, subject, spec: Spec) -> JobRun:
        run_id = (definition.task_id_for(subject, spec) if definition.stable_task_id else None) or str(uuid.uuid4())
        run = JobRun.objects.select_for_update().filter(pk=run_id).first()
        now = timezone.now()
        if run is None:
            run = JobRun(id=run_id)
        else:
            # A stable task id: the run row starts over; its previous attempt stays in job_events.
            run.received_at = run.started_at = run.finished_at = run.sent_at = None
            run.result = run.error = run.traceback = run.report = None
            run.reported_at = None
            run.args_exact = False
        run.job = job
        run.name = definition.task
        run.status = RunStatus.QUEUED
        run.queue = definition.queue
        run.kind = job.kind
        run.subject_type_id, run.subject_id = job.subject_type_id, job.subject_id
        run.catalog_id, run.entry_id = job.catalog_id, job.entry_id
        run.last_event_at = now
        from evil_flowers_catalog.log_format import current_trace_ids

        run.trace_id, _span = current_trace_ids()
        run.save()
        return run

    def _dispatch(self, definition: JobDefinition, job_id, run_id, subject, spec: Spec) -> None:
        try:
            with running_as(str(run_id)):
                definition.dispatch(Dispatch(str(job_id), str(run_id), subject, spec, self.dispatcher))
        except Exception as error:
            message = error.message if isinstance(error, DispatchError) else f"dispatch failed: {error}"
            if not isinstance(error, DispatchError):
                logger.exception("jobs.dispatch_failed kind=%s job=%s run=%s", definition.kind, job_id, run_id)
            now = timezone.now()
            JobRun.objects.filter(pk=run_id, status=RunStatus.QUEUED).update(
                status=RunStatus.FAILED, error=message, finished_at=now, last_event_at=now, updated_at=now
            )
            run = JobRun.objects.filter(pk=run_id).first()
            if run is not None:
                self.run_changed(run)

    # -- facts: runs ----------------------------------------------------------------------------------

    def run_published(self, run: JobRun) -> None:
        """A run the facade did not start (a `.delay()`, beat, a service's follow-up) was sent."""
        if run.job_id is None:
            self._attach(run)

    def run_changed(self, run: JobRun) -> None:
        """A run's status changed (Celery event, sweeper, failed dispatch): move its job, then apply its outcome."""
        self._move_job(run)
        if run.job_id is not None and RunStatus(run.status).final and run.reported_at is None:
            definition = self.registry.for_task(run.name)
            if definition is not None and definition.outcome_file:
                try:
                    self.apply_outcome(run.pk)
                except Exception:
                    # Never into the event listener: the reconcile tick applies it later.
                    logger.exception("jobs.outcome_failed run=%s", run.pk)

    def _move_job(self, run: JobRun) -> None:
        if run.job_id is None and not self._attach(run):
            return
        with transaction.atomic():
            job = (
                Job.objects.select_for_update(of=("self",)).select_related("current_run").filter(pk=run.job_id).first()
            )
            if job is None:
                return
            definition = self.registry.get(job.kind)
            if definition is None or not self._is_current(job, run, definition):
                return
            if job.current_run_id != run.pk:
                job.current_run = run
                job.attempts += 1
            run_status = RunStatus(run.status)
            snapshot = self.snapshot(job, definition, run_status)
            state = machine.on_run(snapshot, run_status)
            if state.settled and definition.observes:
                observation = self._observe(definition, job)
                if observation is not None:
                    state = machine.settle_with_observation(state, observation.state)
                    job.detail = json_safe(dict(observation.detail))
            job.reason = run.error if state in (JobState.FAILED, JobState.PENDING) and run.error else job.reason
            if state is JobState.SUCCEEDED:
                job.reason = None
            self._transition(job, state, f"run:{run_status.value}", run=str(run.pk))
            job.save()

    def _attach(self, run: JobRun) -> bool:
        """Give a run its job: its kind's job (created without intent), or its parent run's job."""
        definition = self.registry.for_task(run.name)
        if definition is not None and definition.keyed:
            subject = self._load_subject(definition, run.subject_id) if definition.subject_model else None
            if definition.subject_model is not None and subject is None:
                return False
            with transaction.atomic():
                job = self._lock_or_create(definition, subject)
                if self._is_new(job):
                    job.state = JobState.QUEUED
                    job.last_transition_at = timezone.now()
                    job.save()
            JobRun.objects.filter(pk=run.pk, job__isnull=True).update(job=job)
            run.job_id = job.pk
            return True
        if run.parent_id:
            parent_job = JobRun.objects.filter(pk=run.parent_id).values_list("job_id", flat=True).first()
            if parent_job:
                JobRun.objects.filter(pk=run.pk, job__isnull=True).update(job_id=parent_job)
                run.job_id = parent_job
        # A child run never moves its job: only its kind's own task does.
        return False

    def _is_current(self, job: Job, run: JobRun, definition: JobDefinition) -> bool:
        if run.name != definition.task:
            return False  # a service's follow-up task: part of the job's history, not its state
        current = job.current_run
        return current is None or current.pk == run.pk or run.created_at >= current.created_at

    # -- facts: domain --------------------------------------------------------------------------------

    def observe(self, kind: str, subject) -> Job | None:
        """The domain changed (e.g. `EncryptedContent` saved): bring the kind's job in step."""
        definition = self.definition(kind)
        if subject is None:
            return None
        observation = definition.observe(subject)
        if observation is None:
            return None
        with transaction.atomic():
            job = self._lock_or_create(definition, subject)
            job.detail = json_safe(dict(observation.detail))
            run_active = job.current_run_id is not None and job.current_run is not None and job.current_run.is_active
            current = JobState(job.state)
            new = self._is_new(job)
            if new or current.settled or (current.active and not run_active):
                # No intent pending: the domain is the truth (a discovered job never dispatches: generation 0).
                if current is not JobState.CANCELLED or new:
                    job.reason = observation.reason
                    self._transition(job, observation.state, "observed", observed=observation.state.value)
                    if observation.since is not None and (new or job.state != current):
                        job.last_transition_at = observation.since
            job.save()
        return job

    def discover(self, kind: str | None = None) -> int:
        """Create or refresh the jobs of every subject the providers know (deploy, daily)."""
        count = 0
        for definition in self.registry.all():
            if kind and definition.kind != kind:
                continue
            for subject in definition.discover():
                try:
                    if self.observe(definition.kind, subject) is not None:
                        count += 1
                except Exception:
                    logger.exception(
                        "jobs.discover_failed kind=%s subject=%s", definition.kind, getattr(subject, "pk", subject)
                    )
        return count

    def _observe(self, definition: JobDefinition, job: Job):
        subject = self._load_subject(definition, job.subject_id)
        return definition.observe(subject) if subject is not None else None

    # -- facts: outcome files (#169) ------------------------------------------------------------------

    def apply_outcome(self, run_id) -> str | None:
        """Apply the outcome file a finished run's worker wrote to shared storage. Idempotent.

        Returns what happened: `applied`, `rejected` (the file failed a check: the job fails and the run's
        files are deleted), `discarded` (the run was revoked or replaced: its files are deleted), or None when
        there is nothing to apply yet (no file, storage unreadable, already applied). None is retried by the
        reconcile tick; the other three are final (`reported_at` is set).
        """
        run = JobRun.objects.select_related("job").filter(pk=run_id).first()
        if run is None or run.job is None or run.reported_at is not None or RunStatus(run.status).active:
            return None
        definition = self.registry.for_task(run.name)
        if definition is None or not definition.outcome_file:
            return None
        subject = self._load_subject(definition, run.job.subject_id) if definition.subject_model else None
        run_kwargs = dict(run.job_kwargs or {})
        path = run_kwargs.get("outcome") or (
            definition.outcome_path(subject, str(run.pk)) if subject is not None else None
        )
        if not path:
            return None
        try:
            payload, problem = self._read_outcome(path)
        except Exception:
            # Storage unreachable from this process: not the file's fault, the tick tries again.
            logger.warning("jobs.outcome_unreadable run=%s path=%s", run.pk, path, exc_info=True)
            return None
        if payload is None and problem is None:
            return None  # not written yet

        with transaction.atomic():
            run = JobRun.objects.select_for_update().get(pk=run.pk)
            # Before anything else: a duplicate event for an applied (and since replaced) run must never
            # delete what the first apply installed.
            if run.reported_at is not None:
                return None
            job = Job.objects.select_for_update(of=("self",)).get(pk=run.job_id)
            status = (payload or {}).get("status")
            if run.status == RunStatus.REVOKED or job.desired_state == DesiredState.CANCELLED:
                verdict, reason = "discarded", "the job was cancelled"
            elif definition.subject_model is not None and subject is None:
                verdict, reason = "discarded", "the job's subject no longer exists"
            elif job.current_run_id not in (None, run.pk):
                verdict, reason = "discarded", "a newer run replaced this one"
            else:
                problem = problem or self._outcome_problem(run, payload)
                stored = None
                if problem is None:
                    spec = definition.spec_model.model_validate(job.spec)
                    try:
                        with transaction.atomic():
                            stored = definition.on_report(
                                Report(str(job.pk), str(run.pk), subject, payload, spec, run_kwargs, declarer=self)
                            )
                    except DispatchError as error:
                        problem = error.message
                verdict, reason = ("applied", None) if problem is None else ("rejected", problem)

            now = timezone.now()
            if verdict == "applied":
                run.report = json_safe(dict(stored))
                token = payload.get("token")
                transaction.on_commit(lambda: CapabilityTokenService.consume(token, JOB_RUN_OUTCOME))
                target = machine.run_after_outcome(RunStatus(run.status), status)
                if status == "failed" and payload.get("error"):
                    run.error = str(payload["error"])
            else:
                run.report = json_safe(
                    {"status": "outcome_rejected" if verdict == "rejected" else "discarded", "reason": reason}
                    | ({"outcome_status": status} if status else {})
                )
                target = machine.run_after_outcome(RunStatus(run.status), None) if verdict == "rejected" else None
                if verdict == "rejected":
                    run.error = f"outcome rejected: {reason}"
            # The report is stored; the file goes, and so does any output no acquisition serves (failed,
            # skipped, rejected, discarded). Computed after `on_report`, which may have installed the output.
            cleanup = [path, *definition.leftovers(subject, str(run.pk), run_kwargs)]
            if target is not None:
                run.status = target
                run.last_event_at = now
            run.reported_at = now
            run.save()
            JobEvent.objects.create(
                job_ref=job,
                run=run,
                type="reported" if verdict == "applied" else f"outcome_{verdict}",
                timestamp=now,
                data=json_safe({"status": status, "reason": reason}),
            )
            if verdict != "discarded":
                transaction.on_commit(lambda: self.run_changed(run))
            transaction.on_commit(lambda: self._delete_files(cleanup))
        log = logger.info if verdict == "applied" else logger.warning
        log("jobs.outcome kind=%s run=%s verdict=%s reason=%s", definition.kind, run.pk, verdict, reason)
        return verdict

    def apply_pending_outcomes(self, limit: int = 200) -> int:
        """The reconcile tick's fallback for lost events: apply outcome files of finished, unreported runs."""
        from apps.tasks.capability_scopes import outcome_token_ttl

        count = 0
        now = timezone.now()
        for definition in self.registry.all():
            if not definition.outcome_file:
                continue
            # Past twice the token's lifetime a file is never applied; it was rejected (and removed) by then.
            window = timedelta(seconds=2 * outcome_token_ttl(definition.time_limit))
            pending = (
                JobRun.objects.filter(
                    name=definition.task,
                    job__isnull=False,
                    reported_at__isnull=True,
                    status__in=(RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.LOST, RunStatus.REVOKED),
                    created_at__gte=now - window,
                )
                .order_by("-created_at")  # newest first: runs that never wrote a file do not starve them
                .values_list("pk", flat=True)[:limit]
            )
            for run_id in list(pending):
                try:
                    if self.apply_outcome(run_id) is not None:
                        count += 1
                except Exception:
                    logger.exception("jobs.outcome_failed run=%s", run_id)
        return count

    @staticmethod
    def _read_outcome(path: str) -> tuple[dict | None, str | None]:
        """(payload, None) for a valid file; (None, problem) for a file that is not an outcome; (None, None): none yet."""
        import json

        from apps.files.storage import get_storage

        storage = get_storage()
        if not storage.exists(path):
            return None, None
        with storage.open(path, "rb") as handle:
            raw = handle.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            return None, "the outcome file is not JSON"
        if not isinstance(payload, dict):
            return None, "the outcome file is not a JSON object"
        return payload, None

    @staticmethod
    def _outcome_problem(run: JobRun, payload: Mapping[str, Any]) -> str | None:
        if str(payload.get("run_id") or "") != str(run.pk):
            return "the outcome names another run"
        grant = CapabilityTokenService.peek(str(payload.get("token") or ""), JOB_RUN_OUTCOME)
        if grant is None:
            return "the outcome's token is unknown or expired"
        if grant.get("resource_id") != str(run.pk):
            return "the outcome's token belongs to another run"
        return None

    @staticmethod
    def _delete_files(paths) -> None:
        from apps.files.storage import get_storage

        storage = get_storage()
        for path in dict.fromkeys(p for p in paths if p):
            try:
                if storage.exists(path):
                    storage.delete(path)
            except Exception:
                logger.exception("jobs.outcome_cleanup_failed path=%s", path)

    # -- helpers --------------------------------------------------------------------------------------

    def definition(self, kind: str) -> JobDefinition:
        definition = self.registry.get(kind)
        if definition is None:
            raise DispatchError(_("Unknown job kind '%(kind)s'.") % {"kind": kind}, status=404)
        return definition

    def subject(self, definition: JobDefinition, subject_id):
        if definition.subject_model is None:
            if subject_id is not None:
                raise DispatchError(_("Kind '%(kind)s' takes no subject.") % {"kind": definition.kind}, status=422)
            return None
        if subject_id is None:
            raise DispatchError(_("Kind '%(kind)s' needs a subject_id.") % {"kind": definition.kind}, status=422)
        subject = self._load_subject(definition, subject_id)
        if subject is None:
            raise DispatchError(_("Subject %(id)s not found.") % {"id": subject_id}, status=404)
        return subject

    @staticmethod
    def parse_spec(definition: JobDefinition, spec: Mapping) -> Spec:
        try:
            return definition.spec_model.model_validate(dict(spec))
        except ValidationError as error:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
            raise DispatchError(_("Invalid spec: %(problems)s") % {"problems": problems}, status=422) from error

    @staticmethod
    def _load_subject(definition: JobDefinition, subject_id):
        if definition.subject_model is None or subject_id is None:
            return None
        try:
            return definition.subject_model._default_manager.get(pk=subject_id)
        except (definition.subject_model.DoesNotExist, ValueError, TypeError):
            return None

    def _find(self, definition: JobDefinition, subject) -> Job | None:
        return Job.objects.filter(**self._key(definition, subject)).first()

    @staticmethod
    def _key(definition: JobDefinition, subject) -> dict:
        if subject is None:
            return {"kind": definition.kind, "subject_type__isnull": True, "subject_id__isnull": True}
        content_type = ContentType.objects.get_for_model(subject, for_concrete_model=True)
        return {"kind": definition.kind, "subject_type": content_type, "subject_id": subject.pk}

    def _lock_or_create(self, definition: JobDefinition, subject) -> Job:
        job = (
            Job.objects.select_for_update(of=("self",))
            .select_related("current_run")
            .filter(**self._key(definition, subject))
            .first()
        )
        if job is not None:
            return job
        job = Job(kind=definition.kind, state=JobState.PENDING)
        if subject is not None:
            job.subject_type = ContentType.objects.get_for_model(subject, for_concrete_model=True)
            job.subject_id = subject.pk
            scope = definition.scope(subject)
            job.catalog_id, job.entry_id = scope.catalog_id, scope.entry_id
        try:
            with transaction.atomic():
                job.save(force_insert=True)
            job.created_now = True  # for the caller's decisions (a new job has no history)
        except IntegrityError:
            job = Job.objects.select_for_update().get(**self._key(definition, subject))
        return job

    @staticmethod
    def _is_new(job: Job) -> bool:
        return getattr(job, "created_now", False)

    @staticmethod
    def snapshot(job: Job, definition: JobDefinition, run_status: RunStatus | None = None) -> JobSnapshot:
        if run_status is None and job.current_run_id is not None and job.current_run is not None:
            run_status = RunStatus(job.current_run.status)
        return JobSnapshot(
            state=JobState(job.state),
            desired=DesiredState(job.desired_state),
            generation=job.generation,
            observed_generation=job.observed_generation,
            attempts=job.attempts,
            max_attempts=definition.max_attempts,
            run_status=run_status,
            last_transition_at=job.last_transition_at,
        )

    @staticmethod
    def _transition(job: Job, state: JobState, event: str, **data) -> None:
        now = timezone.now()
        changed = job.state != state
        job.state = state
        if changed or job.last_transition_at is None:
            job.last_transition_at = now
        # Every change is history; an apply is history even when it changes nothing (it was asked).
        if changed or event == "applied":
            JobEvent.objects.create(job_ref_id=job.pk, type=f"job:{event}", timestamp=now, data=json_safe(data))


facade = JobFacade()

__all__ = ["JobFacade", "Plan", "facade", "is_stale"]
