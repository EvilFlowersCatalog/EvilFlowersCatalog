"""Background jobs (IP-031): list, inspect, start, retry, revoke, act on and report jobs.

Administrators only (each kind's permission, `core.change_entry` by default), except the report
endpoint, which a service worker calls with an API key holding `tasks.report_job`.
"""

import json
from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.filters.jobs import JobFilter
from apps.api.forms.jobs import JobActionForm, JobRetryForm, JobRevokeForm, JobStartForm
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.api.serializers.jobs import JobSerializer, job_detail
from apps.core.errors import ProblemDetailException, ValidationException
from apps.core.views import SecuredView
from apps.tasks.kinds import JobRefused, registry
from apps.tasks.models import JobProtocol
from apps.tasks.services import JobService


def _refused(error: JobRefused) -> ProblemDetailException:
    return ProblemDetailException(error.message, status=error.status, previous=error)


def _require_admin(request):
    try:
        JobService.require(request.user)
    except JobRefused as error:
        raise _refused(error)


def _job(job_id: UUID) -> JobProtocol:
    try:
        return JobProtocol.objects.get(pk=job_id)
    except JobProtocol.DoesNotExist as error:
        raise ProblemDetailException(_("Job not found"), status=HTTPStatus.NOT_FOUND, previous=error)


def _form(form_class, request):
    form = form_class.create_from_request(request)
    if not form.is_valid():
        raise ValidationException(form)
    return form.cleaned_data


class JobManagement(SecuredView):
    @openapi.metadata(
        description=(
            "List background jobs: every Celery task the catalog or a service worker ran, newest first. Filter "
            "by `kind`, `status`, `stale`, `catalog_id`, `entry_id`, subject or title; `latest=true` keeps the "
            "newest job per kind and subject (the subject's current state). Administrators only."
        ),
        tags=["Jobs"],
        summary="List jobs",
    )
    def get(self, request):
        _require_admin(request)
        jobs = JobFilter(request.GET, queryset=JobProtocol.objects.all(), request=request).qs
        ordering = Ordering.create_from_request(request) if "order_by" in request.GET else Ordering(["-created_at"])
        return PaginationResponse(request, jobs, serializer=JobSerializer.Base, ordering=ordering)

    @openapi.metadata(
        description=(
            "Start a job of `kind` on `subject_id` (`GET /api/v1/jobs/kinds` lists kinds, their subject type and "
            "options). The job is recorded as queued before the task is sent. 409 when a job of the kind is already "
            "active for the subject (pass `force` to start anyway), 422 when the subject is not eligible."
        ),
        tags=["Jobs"],
        summary="Start a job",
    )
    def post(self, request):
        _require_admin(request)
        data = _form(JobStartForm, request)
        try:
            job = JobService.start(
                data["kind"],
                data.get("subject_id"),
                options=data.get("options") or {},
                user=request.user,
                force=bool(data.get("force")),
            )
        except JobRefused as error:
            raise _refused(error)
        return SingleResponse(request, data=job_detail(job), status=HTTPStatus.CREATED)


class JobKinds(SecuredView):
    @openapi.metadata(
        description="The registered job kinds: subject type, whether they can be started, options and actions.",
        tags=["Jobs"],
        summary="List job kinds",
    )
    def get(self, request):
        _require_admin(request)
        kinds = [JobSerializer.Kind.model_validate(kind.describe()) for kind in registry.all()]
        return SingleResponse(request, data=kinds)


class JobSummary(SecuredView):
    @openapi.metadata(
        description=(
            "Job counts per kind and status, plus stale queued jobs, for dashboard badges. Takes the list's "
            "filters (e.g. `catalog_id`, `created_after`)."
        ),
        tags=["Jobs"],
        summary="Job summary",
    )
    def get(self, request):
        _require_admin(request)
        jobs = JobFilter(request.GET, queryset=JobProtocol.objects.all(), request=request).qs
        rows = [JobSerializer.Summary.model_validate(row) for row in JobService.summary(jobs)]
        return SingleResponse(request, data=rows)


class JobDetail(SecuredView):
    @openapi.metadata(
        description="One job with its events, children (follow-up tasks), retries and the kind's own fields.",
        tags=["Jobs"],
        summary="Get a job",
    )
    def get(self, request, job_id: UUID):
        _require_admin(request)
        return SingleResponse(request, data=job_detail(_job(job_id)))


class JobRetry(SecuredView):
    @openapi.metadata(
        description=(
            "Run a finished job again as a new job (`retry_of`). Kinds start again with the same options; a task "
            "without a kind is re-sent with its recorded arguments when they were recorded exactly."
        ),
        tags=["Jobs"],
        summary="Retry a job",
    )
    def post(self, request, job_id: UUID):
        data = _form(JobRetryForm, request)
        try:
            job = JobService.retry(_job(job_id), user=request.user, options=data.get("options") or {})
        except JobRefused as error:
            raise _refused(error)
        return SingleResponse(request, data=job_detail(job), status=HTTPStatus.CREATED)


class JobRevoke(SecuredView):
    @openapi.metadata(
        description=(
            "Stop a job that has not started: it is marked revoked at once and no worker runs it. A started job "
            "is stopped only with `terminate`, and only for kinds that can be terminated safely."
        ),
        tags=["Jobs"],
        summary="Revoke a job",
    )
    def post(self, request, job_id: UUID):
        data = _form(JobRevokeForm, request)
        try:
            job = JobService.revoke(_job(job_id), user=request.user, terminate=bool(data.get("terminate")))
        except JobRefused as error:
            raise _refused(error)
        return SingleResponse(request, data=job_detail(job), status=HTTPStatus.ACCEPTED)


class JobActionView(SecuredView):
    @openapi.metadata(
        description=(
            "Run a kind-specific action on a job, e.g. `requeue` or `reencrypt` (with `dry_run`, `force`) for "
            "`readium.encrypt` (#148). Actions are listed per kind in `GET /api/v1/jobs/kinds`."
        ),
        tags=["Jobs"],
        summary="Run a job action",
    )
    def post(self, request, job_id: UUID, action: str):
        data = _form(JobActionForm, request)
        try:
            result = JobService.run_action(_job(job_id), action, user=request.user, options=data.get("options") or {})
        except JobRefused as error:
            raise _refused(error)
        return SingleResponse(request, data=result)


class JobReport(SecuredView):
    @openapi.metadata(
        description=(
            "A service worker reports the outcome of its job; the kind applies it (e.g. OCR replaces the file). "
            "Needs an API key with `tasks.report_job`. Idempotent; 409 for a revoked job."
        ),
        tags=["Jobs"],
        summary="Report a job's outcome",
    )
    def post(self, request, job_id: UUID):
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError as error:
            raise ProblemDetailException(_("The report must be JSON."), status=HTTPStatus.BAD_REQUEST, previous=error)
        if not isinstance(payload, dict):
            raise ProblemDetailException(_("The report must be a JSON object."), status=HTTPStatus.BAD_REQUEST)
        try:
            job = JobService.report(_job(job_id), payload, user=request.user)
        except JobRefused as error:
            raise _refused(error)
        return SingleResponse(request, data={"id": str(job.pk), "report": job.report, "reported_at": job.reported_at})
