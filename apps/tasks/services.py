"""JobService (IP-031): the only way to start, retry, revoke, act on or report a job.

Every start writes the ledger row first and commits it, then dispatches under the row's id, so a job
is visible as queued before any worker can take it. The REST API, the MCP tools and domain triggers
all come through here; the event listener only updates rows.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.tasks import ledger
from apps.tasks.kinds import JobKind, JobRefused, _preassigned, registry
from apps.tasks.models import JobProtocol

logger = logging.getLogger("apps.tasks.audit")

Status = JobProtocol.JobStatus


def _celery():
    from evil_flowers_catalog.celery import app

    return app


def is_stale(job: JobProtocol, now=None) -> bool:
    """Queued longer than `EVILFLOWERS_JOBS_STALE_MINUTES`: almost certainly never reached a worker."""
    if job.status != Status.QUEUED:
        return False
    since = job.sent_at or job.created_at
    if since is None:
        return False
    now = now or timezone.now()
    return now - since > timedelta(minutes=settings.EVILFLOWERS_JOBS_STALE_MINUTES)


def stale_q(now=None) -> Q:
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=settings.EVILFLOWERS_JOBS_STALE_MINUTES)
    return Q(status=Status.QUEUED) & (Q(sent_at__lt=cutoff) | Q(sent_at__isnull=True, created_at__lt=cutoff))


def latest_ids():
    """Ids of the newest job per kind and subject (the "current state" of each subject)."""
    return (
        JobProtocol.objects.filter(subject_id__isnull=False, kind__isnull=False)
        .order_by("kind", "subject_type_id", "subject_id", "-created_at")
        .distinct("kind", "subject_type_id", "subject_id")
        .values("id")
    )


class JobService:
    # -- permissions --------------------------------------------------------------------------------

    @staticmethod
    def require(user, kind: Optional[JobKind] = None) -> None:
        permission = kind.permission if kind is not None else "core.change_entry"
        if user is None or not user.is_authenticated or not user.has_perm(permission):
            raise JobRefused(_("Background jobs are restricted to administrators."), status=403)

    @staticmethod
    def kind(name: Optional[str]) -> JobKind:
        kind = registry.get(name)
        if kind is None:
            raise JobRefused(_("Unknown job kind '%(kind)s'.") % {"kind": name}, status=404)
        return kind

    # -- start --------------------------------------------------------------------------------------

    @classmethod
    def start(
        cls,
        kind_name: str,
        subject_id=None,
        *,
        options: Optional[dict] = None,
        user=None,
        force: bool = False,
        retry_of: Optional[JobProtocol] = None,
        parent: Optional[JobProtocol] = None,
        check_permission: bool = True,
    ) -> JobProtocol:
        """Start one job of `kind_name` on `subject_id`.

        `check_permission=False` is for domain triggers (an upload starting OCR), which act for the
        system, not for a user.
        """
        kind = cls.kind(kind_name)
        if check_permission:
            cls.require(user, kind)
        if not kind.startable and retry_of is None:
            raise JobRefused(_("Jobs of kind '%(kind)s' cannot be started by hand.") % {"kind": kind.name})

        options = dict(options or {})
        cls._check_options(kind, options)

        subject = None
        if kind.subject_model is not None:
            if subject_id is None:
                raise JobRefused(_("Kind '%(kind)s' needs a subject_id.") % {"kind": kind.name}, status=422)
            subject = kind.get_subject(subject_id)
            if subject is None:
                raise JobRefused(_("Subject %(id)s not found.") % {"id": subject_id}, status=404)

        reason = kind.eligible(subject, options)
        if reason:
            raise JobRefused(reason, status=422)

        stable_id = kind.task_id_for(subject, options) if kind.stable_task_id else None
        if kind.exclusive and not force:
            active = cls.active(kind, subject)
            if stable_id:
                active = active.exclude(pk=stable_id)
            active = active.first()
            if active is not None:
                raise JobRefused(
                    _("A %(kind)s job for this subject is already %(status)s (%(id)s).")
                    % {"kind": kind.name, "status": active.status, "id": active.pk}
                )

        job = cls._write_row(kind, subject, options, user, retry_of, stable_id, parent)
        cls._dispatch(kind, job, subject, options)
        logger.info(
            "jobs.start kind=%s job=%s subject=%s user=%s retry_of=%s force=%s",
            kind.name,
            job.pk,
            job.subject_id,
            getattr(user, "pk", None),
            getattr(retry_of, "pk", None),
            force,
        )
        return job

    @staticmethod
    def _check_options(kind: JobKind, options: dict) -> None:
        schema = kind.options_schema or {}
        allowed = set((schema.get("properties") or {}).keys())
        unknown = sorted(set(options) - allowed)
        if unknown and schema.get("additionalProperties", True) is False:
            raise JobRefused(
                _("Unknown option(s) for %(kind)s: %(unknown)s.") % {"kind": kind.name, "unknown": ", ".join(unknown)},
                status=422,
            )

    @staticmethod
    def active(kind: JobKind, subject):
        queryset = JobProtocol.objects.filter(kind=kind.name, status__in=JobProtocol.ACTIVE_STATUSES)
        if subject is not None:
            queryset = queryset.filter(subject_id=subject.pk)
        return queryset.order_by("-created_at")

    @staticmethod
    def _write_row(kind, subject, options, user, retry_of, stable_id, parent=None) -> JobProtocol:
        from evil_flowers_catalog.log_format import current_trace_ids

        trace_id, _span = current_trace_ids()
        now = timezone.now()
        with transaction.atomic():
            job = None
            if stable_id:
                job = JobProtocol.objects.select_for_update().filter(pk=stable_id).first()
            if job is None:
                job = JobProtocol(id=stable_id or uuid.uuid4(), name=kind.task)
            else:
                # A stable-id kind re-sends the same task id: the row starts over. The previous attempt
                # stays in `job_events`; its late events are older than `last_event_at` and ignored.
                job.result = job.error = job.traceback = job.report = None
                job.reported_at = job.revoke_requested_at = None
                job.received_at = job.started_at = job.finished_at = job.sent_at = None
                job.last_event_at = now
            job.kind = kind.name
            job.status = Status.QUEUED
            job.queue = kind.queue or job.queue
            job.options = ledger.json_safe(options)
            job.requested_by = user if getattr(user, "is_authenticated", False) else None
            job.retry_of = retry_of if retry_of is not None and str(retry_of.pk) != str(job.pk) else None
            job.trace_id = trace_id
            if parent is not None:
                job.parent_id = parent.pk
            if subject is not None:
                ledger.set_subject(job, kind, subject)
            job.save()
            ledger.log(
                job,
                "queued",
                user=str(user.pk) if getattr(user, "is_authenticated", False) else None,
                retry_of=str(retry_of.pk) if retry_of is not None else None,
            )
        return job

    @staticmethod
    def _dispatch(kind: JobKind, job: JobProtocol, subject, options: dict) -> None:
        token = _preassigned.set(str(job.pk))
        try:
            kind.dispatch(job, subject, options)
        except JobRefused as error:
            JobService._fail(job, error.message)
            raise
        except Exception as error:
            logger.exception("jobs.dispatch_failed kind=%s job=%s", kind.name, job.pk)
            JobService._fail(job, f"dispatch failed: {error}")
            raise JobRefused(_("The job could not be sent: %(error)s") % {"error": error}, status=502)
        finally:
            _preassigned.reset(token)

    @staticmethod
    def _fail(job: JobProtocol, message: str) -> None:
        JobProtocol.objects.filter(pk=job.pk, status=Status.QUEUED).update(
            status=Status.FAILED, error=message, finished_at=timezone.now(), updated_at=timezone.now()
        )
        job.refresh_from_db()
        ledger.log(job, "dispatch-failed", error=message)

    # -- retry --------------------------------------------------------------------------------------

    @classmethod
    def retry(cls, job: JobProtocol, *, user=None, options: Optional[dict] = None) -> JobProtocol:
        """Run `job` again: through its kind when it has one that can start, else re-send the call."""
        kind = registry.get(job.kind)
        cls.require(user, kind)
        if job.is_active:
            raise JobRefused(_("The job is still %(status)s.") % {"status": job.status})

        if kind is not None and kind.startable and (kind.subject_model is None or job.subject_id):
            merged = {**kind.retry_options(job), **(options or {})}
            return cls.start(kind.name, job.subject_id, options=merged, user=user, retry_of=job)

        if not job.args_exact:
            raise JobRefused(
                _("This job cannot be re-sent: its arguments were not recorded exactly (or held a secret).")
            )
        if kind is None and job.name not in _celery().tasks:
            raise JobRefused(_("Only the catalog's own tasks can be re-sent without a job kind."))

        new = JobProtocol(
            id=uuid.uuid4(),
            name=job.name,
            kind=job.kind,
            queue=job.queue,
            subject_type_id=job.subject_type_id,
            subject_id=job.subject_id,
            catalog_id=job.catalog_id,
            entry_id=job.entry_id,
            job_args=job.job_args,
            job_kwargs=job.job_kwargs,
            args_exact=True,
            options=job.options,
            retry_of=job,
            requested_by=user if getattr(user, "is_authenticated", False) else None,
            status=Status.QUEUED,
        )
        with transaction.atomic():
            new.save(force_insert=True)
            ledger.log(new, "queued", user=str(getattr(user, "pk", "")) or None, retry_of=str(job.pk))
        try:
            _celery().send_task(
                job.name, args=job.job_args, kwargs=job.job_kwargs, task_id=str(new.pk), queue=job.queue
            )
        except Exception as error:
            logger.exception("jobs.resend_failed job=%s", job.pk)
            cls._fail(new, f"dispatch failed: {error}")
            raise JobRefused(_("The job could not be sent: %(error)s") % {"error": error}, status=502)
        logger.info("jobs.resend job=%s new=%s user=%s", job.pk, new.pk, getattr(user, "pk", None))
        return new

    # -- revoke -------------------------------------------------------------------------------------

    @classmethod
    def revoke(cls, job: JobProtocol, *, user=None, terminate: bool = False) -> JobProtocol:
        """Stop `job` from running (Q7): a queued or received job never starts; a started one is
        killed only with `terminate` and only for kinds that declare it safe."""
        kind = registry.get(job.kind)
        cls.require(user, kind)
        if not job.is_active:
            raise JobRefused(_("The job already finished (%(status)s).") % {"status": job.status})
        if job.status == Status.STARTED and not terminate:
            raise JobRefused(_("The job is already running; pass terminate to stop it."))
        if kind is not None and kind.stable_task_id:
            # Workers remember revoked ids for hours; a re-run under the same id would be discarded too.
            raise JobRefused(_("Jobs of this kind reuse their task id and cannot be revoked."), status=422)
        if terminate and not (kind is not None and kind.terminate_safe):
            raise JobRefused(
                _("Jobs of this kind cannot be terminated safely while running (their worker writes files)."),
                status=422,
            )

        _celery().control.revoke(str(job.pk), terminate=terminate)
        now = timezone.now()
        with transaction.atomic():
            job = JobProtocol.objects.select_for_update().get(pk=job.pk)
            job.revoke_requested_at = now
            if job.status in (Status.QUEUED, Status.RECEIVED, Status.RETRYING):
                job.status = Status.REVOKED
                job.finished_at = now
                job.error = job.error or "revoked"
            job.save()
            ledger.log(job, "revoke-requested", user=str(getattr(user, "pk", "")) or None, terminate=terminate)
        logger.info("jobs.revoke job=%s user=%s terminate=%s", job.pk, getattr(user, "pk", None), terminate)
        return job

    # -- actions ------------------------------------------------------------------------------------

    @classmethod
    def run_action(cls, job: JobProtocol, action_name: str, *, user=None, options: Optional[dict] = None) -> dict:
        kind = registry.get(job.kind)
        cls.require(user, kind)
        if kind is None or action_name not in kind.actions:
            raise JobRefused(_("Unknown action '%(action)s' for this job.") % {"action": action_name}, status=404)
        action = kind.actions[action_name]
        subject = kind.get_subject(job.subject_id)
        if kind.subject_model is not None and subject is None:
            raise JobRefused(_("The job's subject no longer exists."), status=404)
        result = action.handler(job, subject, dict(options or {}), user)
        if not action.read_only:
            ledger.log(job, f"action:{action_name}", user=str(getattr(user, "pk", "")) or None, options=options or {})
            logger.info("jobs.action job=%s action=%s user=%s", job.pk, action_name, getattr(user, "pk", None))
        return result

    # -- report -------------------------------------------------------------------------------------

    @classmethod
    def report(cls, job: JobProtocol, payload: dict, *, user=None) -> JobProtocol:
        """Store a worker's report and let the kind apply it to the subject (idempotent)."""
        if user is None or not user.is_authenticated or not user.has_perm("tasks.report_job"):
            raise JobRefused(_("Reporting a job needs the tasks.report_job permission."), status=403)
        kind = registry.get(job.kind)
        if kind is None or not kind.reports:
            raise JobRefused(_("This job does not take reports."), status=400)
        if job.status == Status.REVOKED or job.revoke_requested_at:
            raise JobRefused(_("The job was revoked; its report is not applied."))

        with transaction.atomic():
            job = JobProtocol.objects.select_for_update().get(pk=job.pk)
            if job.reported_at:
                return job
            subject = kind.get_subject(job.subject_id)
            if kind.subject_model is not None and subject is None:
                raise JobRefused(_("The job's subject no longer exists."), status=404)
            stored = kind.on_report(job, subject, payload)
            job.report = ledger.json_safe(stored)
            job.reported_at = timezone.now()
            job.save(update_fields=["report", "reported_at", "updated_at"])
            ledger.log(job, "reported", status=(payload or {}).get("status"))
        return job

    # -- summary ------------------------------------------------------------------------------------

    @staticmethod
    def summary(queryset) -> list:
        """Counts per kind and status, plus stale, for the dashboard's attention badges."""
        now = timezone.now()
        rows = queryset.values("kind", "status").annotate(count=Count("id")).order_by("kind", "status")
        stale = dict(
            queryset.filter(stale_q(now)).values("kind").annotate(count=Count("id")).values_list("kind", "count")
        )
        result = {}
        for row in rows:
            entry = result.setdefault(row["kind"], {"kind": row["kind"], "statuses": {}, "stale": 0})
            entry["statuses"][row["status"]] = row["count"]
        for kind, count in stale.items():
            result.setdefault(kind, {"kind": kind, "statuses": {}, "stale": 0})["stale"] = count
        return list(result.values())


__all__ = ["JobService", "is_stale", "latest_ids", "stale_q"]
