"""IP-035: the live jobs dashboard (`console/jobs/`) over the IP-031 jobs ledger.

Reads aggregate in SQL over bounded windows (`job_protocols` holds every Celery run, tens of thousands of
rows). Writes go only through the declarative facade (`apps.tasks.application.facade`), the same calls as
`PATCH /api/v1/jobs/{id}` and MCP `apply_job`:

* **retry** — apply the job's spec again with `desired_state: completed`; a settled job gets a new
  generation, an active one keeps running (IP-031 Q12);
* **cancel** — `desired_state: cancelled`; the reconciler cancels, revokes or stops;
* **reconcile** — take the one transition the state machine allows now (what beat does every minute).

`EVILFLOWERS_ADMIN_READ_ONLY` refuses every action with 403.
"""

from datetime import timedelta
from typing import Dict, List, Optional
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.urls import NoReverseMatch, path, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.console.options import read_only_deployment
from apps.console.views import ApiError, ConsolePage, ConsoleApi, bounded_int
from apps.tasks.domain.values import DesiredState, JobState, RunStatus
from apps.tasks.models import Job, JobEvent, JobRun

MODULE = "jobs"
#: Run statuses counted as failures in the failure rate.
FAILED_RUNS = (RunStatus.FAILED.value, RunStatus.REJECTED.value, RunStatus.LOST.value)
ACTIONS = ("retry", "cancel", "reconcile")
PAGE_SIZE = 50


def _admin_url(name: str, *args) -> Optional[str]:
    try:
        return reverse(f"admin:{name}", args=args)
    except NoReverseMatch:
        return None


def _trace_url(trace_id: Optional[str]) -> Optional[str]:
    """`EVILFLOWERS_TRACE_URL_TEMPLATE` (e.g. a Grafana Explore link with `{trace_id}`), when a deployment sets one."""
    template = getattr(settings, "EVILFLOWERS_TRACE_URL_TEMPLATE", "")
    if not trace_id or not template:
        return None
    return template.replace("{trace_id}", trace_id)


def _iso(value):
    return value.isoformat() if value else None


class JobsPage(ConsolePage):
    template_name = "console/jobs.html"
    module = MODULE
    title = _("Jobs")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["read_only"] = read_only_deployment()
        context["job_states"] = [state.value for state in JobState]
        context["run_statuses"] = [status.value for status in RunStatus]
        context["kinds"] = list(Job.objects.order_by().values_list("kind", flat=True).distinct())
        return context


class JobsSummary(ConsoleApi):
    """KPIs: jobs per state, runs in the last hour and day, failure rate and runtime percentiles (24 h)."""

    module = MODULE

    def get_data(self, request):
        now = timezone.now()
        states = dict(Job.objects.order_by().values_list("state").annotate(n=Count("id")).values_list("state", "n"))
        stale_cutoff = now - timedelta(minutes=settings.EVILFLOWERS_JOBS_STALE_MINUTES)
        stale = Job.objects.filter(state=JobState.QUEUED, last_transition_at__lt=stale_cutoff).count()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (WHERE created_at >= %(hour)s),
                    count(*),
                    count(*) FILTER (WHERE status = ANY(%(failed)s)),
                    count(*) FILTER (WHERE status = ANY(%(final)s)),
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY runtime) FILTER (WHERE runtime IS NOT NULL),
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY runtime) FILTER (WHERE runtime IS NOT NULL),
                    count(*) FILTER (WHERE status = ANY(%(active)s))
                FROM job_protocols
                WHERE created_at >= %(day)s
                """,
                {
                    "hour": now - timedelta(hours=1),
                    "day": now - timedelta(hours=24),
                    "failed": list(FAILED_RUNS),
                    "final": [status.value for status in RunStatus if status.final],
                    "active": [status.value for status in RunStatus if status.active],
                },
            )
            last_hour, last_day, failed, final, p50, p95, active = cursor.fetchone()

        return {
            "now": now.isoformat(),
            "states": {state.value: states.get(state.value, 0) for state in JobState},
            "jobs_total": sum(states.values()),
            "stale": stale,
            "runs": {
                "last_hour": last_hour,
                "last_day": last_day,
                "active_last_day": active,
                "failed_last_day": failed,
                "failure_rate": (failed / final) if final else None,
                "runtime_p50": p50,
                "runtime_p95": p95,
            },
            "read_only": read_only_deployment(),
        }


class JobsTimeseries(ConsoleApi):
    """Charts: runs per bucket stacked by status, runtime per kind, and the job state × kind matrix."""

    module = MODULE

    def get_data(self, request):
        hours = bounded_int(request.GET.get("hours"), 24, 1, 24 * 14)
        bucket_minutes = 10 if hours <= 24 else 60
        now = timezone.now()
        since = now - timedelta(hours=hours)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT date_bin(%(step)s, created_at, %(origin)s) AS bucket, status, count(*)
                FROM job_protocols
                WHERE created_at >= %(since)s
                GROUP BY 1, 2
                ORDER BY 1
                """,
                {"step": timedelta(minutes=bucket_minutes), "origin": since, "since": since},
            )
            rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT coalesce(kind, name) AS label,
                       count(*),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY runtime),
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY runtime),
                       max(runtime)
                FROM job_protocols
                WHERE created_at >= %(since)s AND runtime IS NOT NULL
                GROUP BY 1
                ORDER BY 2 DESC
                LIMIT 20
                """,
                {"since": since},
            )
            runtimes = [
                {"kind": label, "runs": count, "p50": p50, "p95": p95, "max": high}
                for label, count, p50, p95, high in cursor.fetchall()
            ]

        buckets: List[str] = []
        step = timedelta(minutes=bucket_minutes)
        cursor_time = since
        while cursor_time <= now:
            buckets.append(cursor_time.isoformat())
            cursor_time += step
        index = {value: position for position, value in enumerate(buckets)}
        statuses: Dict[str, List[int]] = {}
        for bucket, status, count in rows:
            position = index.get(bucket.isoformat())
            if position is None:
                continue
            statuses.setdefault(status, [0] * len(buckets))[position] = count

        matrix = list(
            Job.objects.order_by().values("kind", "state").annotate(n=Count("id")).values_list("kind", "state", "n")
        )
        kinds = sorted({kind or "—" for kind, _, _ in matrix})
        return {
            "hours": hours,
            "bucket_minutes": bucket_minutes,
            "buckets": buckets,
            "series": statuses,
            "runtimes": runtimes,
            "matrix": {
                "kinds": kinds,
                "states": [state.value for state in JobState],
                "cells": [[kind or "—", state, n] for kind, state, n in matrix],
            },
        }


class JobsList(ConsoleApi):
    """Recent jobs (`view=jobs`, by last transition) or runs (`view=runs`, newest first), filterable."""

    module = MODULE

    def get_data(self, request):
        view = request.GET.get("view", "jobs")
        page = bounded_int(request.GET.get("page"), 1, 1, 10_000)
        kind = request.GET.get("kind") or None
        state = request.GET.get("state") or None
        query = (request.GET.get("q") or "").strip()
        offset = (page - 1) * PAGE_SIZE
        if view == "runs":
            return self._runs(kind, state, query, offset, page)
        return self._jobs(kind, state, query, offset, page)

    def _jobs(self, kind, state, query, offset, page):
        queryset = Job.objects.select_related("entry", "current_run").order_by("-last_transition_at", "-created_at")
        if kind:
            queryset = queryset.filter(kind=kind)
        if state:
            queryset = queryset.filter(state=state)
        if query:
            queryset = queryset.filter(Q(entry__title__icontains=query) | Q(reason__icontains=query))
        rows = list(queryset[offset : offset + PAGE_SIZE + 1])
        return {
            "view": "jobs",
            "page": page,
            "has_next": len(rows) > PAGE_SIZE,
            "rows": [
                {
                    "id": str(job.pk),
                    "kind": job.kind,
                    "state": job.state,
                    "desired_state": job.desired_state,
                    "entry": job.entry.title if job.entry_id and job.entry else None,
                    "entry_url": _admin_url("core_entry_change", job.entry_id) if job.entry_id else None,
                    "attempts": job.attempts,
                    "generation": job.generation,
                    "reason": job.reason,
                    "last_transition_at": _iso(job.last_transition_at),
                    "worker": job.current_run.worker if job.current_run else None,
                    "runtime": job.current_run.runtime if job.current_run else None,
                    "run_status": job.current_run.status if job.current_run else None,
                }
                for job in rows[:PAGE_SIZE]
            ],
        }

    def _runs(self, kind, state, query, offset, page):
        queryset = (
            JobRun.objects.select_related("entry")
            .defer("job_args", "job_kwargs", "result", "report", "traceback")
            .order_by("-created_at")
        )
        if kind:
            queryset = queryset.filter(kind=kind)
        if state:
            queryset = queryset.filter(status=state)
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(trace_id=query) | Q(worker__icontains=query))
        rows = list(queryset[offset : offset + PAGE_SIZE + 1])
        return {
            "view": "runs",
            "page": page,
            "has_next": len(rows) > PAGE_SIZE,
            "rows": [
                {
                    "id": str(run.pk),
                    "job_id": str(run.job_id) if run.job_id else None,
                    "kind": run.kind or run.name,
                    "state": run.status,
                    "entry": run.entry.title if run.entry_id and run.entry else None,
                    "entry_url": _admin_url("core_entry_change", run.entry_id) if run.entry_id else None,
                    "attempts": run.attempts,
                    "queue": run.queue,
                    "worker": run.worker,
                    "runtime": run.runtime,
                    "created_at": _iso(run.created_at),
                    "last_transition_at": _iso(run.finished_at or run.started_at or run.created_at),
                    "error": run.error,
                }
                for run in rows[:PAGE_SIZE]
            ],
        }


def _run_payload(run: JobRun, with_traceback: bool = False) -> dict:
    payload = {
        "id": str(run.pk),
        "name": run.name,
        "kind": run.kind,
        "status": run.status,
        "queue": run.queue,
        "worker": run.worker,
        "attempts": run.attempts,
        "runtime": run.runtime,
        "error": run.error,
        "trace_id": run.trace_id,
        "trace_url": _trace_url(run.trace_id),
        "sent_at": _iso(run.sent_at),
        "received_at": _iso(run.received_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
        "admin_url": _admin_url("tasks_jobrun_change", run.pk),
    }
    if with_traceback:
        payload["traceback"] = run.traceback
        payload["result"] = run.result
        payload["report"] = run.report
    return payload


def _events(filter_q, limit: int = 300) -> List[dict]:
    events = JobEvent.objects.filter(filter_q).order_by("-timestamp")[:limit]
    return [
        {
            "type": event.type,
            "timestamp": _iso(event.timestamp),
            "hostname": event.hostname,
            "run_id": str(event.run_id) if event.run_id else None,
            "data": event.data,
        }
        for event in reversed(list(events))
    ]


class JobDetail(ConsoleApi):
    """One job: spec, detail, reason, its runs and the events of the job and its runs."""

    module = MODULE

    def get_data(self, request, job_id: UUID):
        job = Job.objects.select_related("entry", "catalog", "current_run", "requested_by").filter(pk=job_id).first()
        if job is None:
            raise ApiError("Job not found.", status=404)
        runs = list(
            JobRun.objects.filter(job=job)
            .defer("job_args", "job_kwargs", "result", "report")
            .order_by("-created_at")[:20]
        )
        run_ids = [run.pk for run in runs]
        current = job.current_run
        return {
            "id": str(job.pk),
            "kind": job.kind,
            "state": job.state,
            "desired_state": job.desired_state,
            "reason": job.reason,
            "spec": job.spec,
            "detail": job.detail,
            "generation": job.generation,
            "observed_generation": job.observed_generation,
            "attempts": job.attempts,
            "stale": job.stale,
            "subject": job.subject_label,
            "subject_id": str(job.subject_id) if job.subject_id else None,
            "entry": job.entry.title if job.entry else None,
            "entry_url": _admin_url("core_entry_change", job.entry_id) if job.entry_id else None,
            "catalog": job.catalog.title if job.catalog else None,
            "requested_by": job.requested_by.username if job.requested_by else None,
            "last_transition_at": _iso(job.last_transition_at),
            "created_at": _iso(job.created_at),
            "admin_url": _admin_url("tasks_job_change", job.pk),
            "current_run": _run_payload(current, with_traceback=True) if current else None,
            "runs": [_run_payload(run, with_traceback=run.status in FAILED_RUNS) for run in runs],
            "events": _events(Q(job_ref=job) | Q(run_id__in=run_ids)),
            "actions": [] if read_only_deployment() else self._actions(job),
        }

    @staticmethod
    def _actions(job: Job) -> List[str]:
        state = JobState(job.state)
        actions = ["reconcile"]
        if state.settled or job.desired_state == DesiredState.CANCELLED or state is JobState.BLOCKED:
            actions.insert(0, "retry")
        if not state.settled and job.desired_state != DesiredState.CANCELLED:
            actions.insert(0, "cancel")
        return actions


class RunDetail(ConsoleApi):
    module = MODULE

    def get_data(self, request, run_id: UUID):
        run = JobRun.objects.filter(pk=run_id).first()
        if run is None:
            raise ApiError("Run not found.", status=404)
        payload = _run_payload(run, with_traceback=True)
        payload["job_id"] = str(run.job_id) if run.job_id else None
        payload["args"] = run.job_args
        payload["kwargs"] = run.job_kwargs
        payload["events"] = _events(Q(run=run))
        return payload


class JobAction(ConsoleApi):
    """POST `{"action": "retry" | "cancel" | "reconcile"}`, through the jobs facade only."""

    module = MODULE

    def post_data(self, request, payload, job_id: UUID):
        from apps.tasks.application.facade import facade
        from apps.tasks.contract import DispatchError

        if read_only_deployment():
            raise ApiError("This deployment's console is read-only (EVILFLOWERS_ADMIN_READ_ONLY).", status=403)
        action = payload.get("action")
        if action not in ACTIONS:
            raise ApiError(f"Unknown action. Known: {', '.join(ACTIONS)}.")
        try:
            if action == "retry":
                job = facade.patch(job_id, desired=DesiredState.COMPLETED, user=request.user)
            elif action == "cancel":
                job = facade.patch(job_id, desired=DesiredState.CANCELLED, user=request.user)
            else:
                facade.require(request.user)
                facade.get(job_id)
                job = facade.reconcile(job_id)
        except DispatchError as error:
            raise ApiError(error.message, status=error.status)
        if job is None:
            raise ApiError("Job not found.", status=404)
        return {
            "action": action,
            "id": str(job.pk),
            "state": job.state,
            "desired_state": job.desired_state,
            "generation": job.generation,
            "reason": job.reason,
        }


class JobsQueues(ConsoleApi):
    """Messages waiting per broker queue: the extraction services' queues and every queue runs used in a day."""

    module = MODULE

    def get_data(self, request):
        from apps.extraction.admin_views import SERVICE_QUEUES, _broker_client

        names: Dict[str, str] = {}
        for service, setting, default in SERVICE_QUEUES:
            names[getattr(settings, setting, default) if setting else default] = service
        since = timezone.now() - timedelta(hours=24)
        for queue in (
            JobRun.objects.filter(created_at__gte=since).order_by().values_list("queue", flat=True).distinct()
        ):
            if queue:
                names.setdefault(queue, queue)

        client = _broker_client()
        result = []
        for queue, service in sorted(names.items()):
            waiting = None
            if client is not None:
                try:
                    waiting = int(client.llen(queue))
                except Exception:
                    waiting = None
            result.append({"queue": queue, "service": service, "waiting": waiting})
        return {"reachable": client is not None, "queues": result}


def urlpatterns(site):
    wrap = site.admin_view
    return [
        path("console/jobs/", wrap(JobsPage.as_view(site=site)), name="console_jobs"),
        path("console/api/jobs/summary/", wrap(JobsSummary.as_view()), name="console_jobs_summary"),
        path("console/api/jobs/timeseries/", wrap(JobsTimeseries.as_view()), name="console_jobs_timeseries"),
        path("console/api/jobs/list/", wrap(JobsList.as_view()), name="console_jobs_list"),
        path("console/api/jobs/queues/", wrap(JobsQueues.as_view()), name="console_jobs_queues"),
        path("console/api/jobs/runs/<uuid:run_id>/", wrap(RunDetail.as_view()), name="console_jobs_run"),
        path("console/api/jobs/<uuid:job_id>/", wrap(JobDetail.as_view()), name="console_jobs_detail"),
        path("console/api/jobs/<uuid:job_id>/action/", wrap(JobAction.as_view()), name="console_jobs_action"),
    ]
