"""`/api/v1/jobs` (IP-031 rev. 2): the one job resource, declarative.

Clients declare (`POST` applies a spec, `PATCH` changes spec or desired state) and read; the
reconciler and the facts move the state. Retry = apply again; cancel = `desired_state: cancelled`.
Administrators only, except the run report, which a service worker posts with `tasks.report_job`.
"""

import json
from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.filters.jobs import JobFilter, RunFilter
from apps.api.forms.jobs import JobApplyForm, JobPatchForm
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.api.serializers.jobs import JobSerializer, RunSerializer
from apps.core.errors import ProblemDetailException, ValidationException
from apps.core.views import SecuredView
from apps.tasks.application.facade import Plan, facade
from apps.tasks.application.registry import registry
from apps.tasks.contract import DispatchError
from apps.tasks.models import JobRun


def _refused(error: DispatchError) -> ProblemDetailException:
    # A helper, not inline: `apps.openapi` reads `status=HTTPStatus.<NAME>` literally from view code.
    return ProblemDetailException(error.message, status=error.status, previous=error)


def _require_admin(request):
    try:
        facade.require(request.user)
    except DispatchError as error:
        raise _refused(error)


def _form(form_class, request) -> dict:
    form = form_class.create_from_request(request)
    if not form.is_valid():
        raise ValidationException(form)
    return form.cleaned_data


def _ordering(request, default: str) -> Ordering:
    return Ordering.create_from_request(request) if "order_by" in request.GET else Ordering([default])


class JobCollection(SecuredView):
    @openapi.metadata(
        description=(
            "Jobs of every kind, one per kind and subject: declared (`spec`, `desired_state`, `generation`) and "
            "observed (`state`, `reason`, `detail`). Filter by `kind`, `state`, `stale`, `catalog_id`, `entry_id`, "
            "subject, `cause_id` or title. Administrators only."
        ),
        tags=["Jobs"],
        summary="List jobs",
    )
    def get(self, request):
        _require_admin(request)
        jobs = JobFilter(request.GET, queryset=facade.jobs(), request=request).qs
        return PaginationResponse(
            request, jobs, serializer=JobSerializer.Base, ordering=_ordering(request, "-last_transition_at")
        )

    @openapi.metadata(
        description=(
            "Declare a job: `{kind, subject_id, spec, desired_state}`. Creates the kind's job for the subject or "
            "updates it; the reconciler then runs it (or blocks it, with `reason`, until it can). The same spec on "
            "a job still on its way changes nothing; on a finished job it runs again (retry). "
            "`desired_state: cancelled` stops it. `?dry_run=true` answers what would happen and stores nothing. "
            "422 when the kind can never run on the subject. Administrators only."
        ),
        tags=["Jobs"],
        summary="Apply a job",
    )
    def post(self, request):
        _require_admin(request)
        data = _form(JobApplyForm, request)
        dry_run = request.GET.get("dry_run", "").lower() == "true"
        try:
            result = facade.apply(
                data["kind"],
                data.get("subject_id"),
                spec=data.get("spec"),
                desired=data.get("desired_state") or "completed",
                user=request.user,
                dry_run=dry_run,
            )
        except DispatchError as error:
            raise _refused(error)
        if isinstance(result, Plan):
            return SingleResponse(request, data=JobSerializer.Plan.from_plan(result))
        return SingleResponse(request, data=JobSerializer.Detail.from_job(result), status=HTTPStatus.CREATED)


class JobDetail(SecuredView):
    @openapi.metadata(
        description="One job with its runs, run counts per task, transitions and the jobs it declared.",
        tags=["Jobs"],
        summary="Get a job",
    )
    def get(self, request, job_id: UUID):
        _require_admin(request)
        try:
            job = facade.get(job_id)
        except DispatchError as error:
            raise _refused(error)
        return SingleResponse(request, data=JobSerializer.Detail.from_job(job))

    @openapi.metadata(
        description=(
            "Change a job's `spec` or `desired_state` (`cancelled` stops it; `completed` resumes it). Same rules "
            "as applying. Administrators only."
        ),
        tags=["Jobs"],
        summary="Change a job",
    )
    def patch(self, request, job_id: UUID):
        _require_admin(request)
        data = _form(JobPatchForm, request)
        try:
            job = facade.patch(
                job_id, spec=data.get("spec"), desired=data.get("desired_state") or None, user=request.user
            )
        except DispatchError as error:
            raise _refused(error)
        return SingleResponse(request, data=JobSerializer.Detail.from_job(job))


class JobKinds(SecuredView):
    @openapi.metadata(
        description="The job kinds apps contribute: subject type, spec schema, whether they report or are run-only.",
        tags=["Jobs"],
        summary="List job kinds",
    )
    def get(self, request):
        _require_admin(request)
        return SingleResponse(
            request, data=[JobSerializer.Kind.model_validate(definition.describe()) for definition in registry.all()]
        )


class JobSummary(SecuredView):
    @openapi.metadata(
        description="Job counts per kind and state, plus stale, for dashboard badges. Takes the list's filters.",
        tags=["Jobs"],
        summary="Job summary",
    )
    def get(self, request):
        _require_admin(request)
        jobs = JobFilter(request.GET, queryset=facade.jobs(), request=request).qs
        return SingleResponse(
            request, data=[JobSerializer.Summary.model_validate(row) for row in facade.summary(jobs)]
        )


class RunCollection(SecuredView):
    @openapi.metadata(
        description=(
            "Runs: every Celery task execution the catalog recorded, with or without a job (`without_job=true`: "
            "run-only kinds and services' tasks). Filter by `job_id`, `kind`, `status`, `name`. Administrators only."
        ),
        tags=["Jobs"],
        summary="List job runs",
    )
    def get(self, request):
        _require_admin(request)
        runs = RunFilter(request.GET, queryset=JobRun.objects.all(), request=request).qs
        return PaginationResponse(
            request, runs, serializer=RunSerializer.Base, ordering=_ordering(request, "-created_at")
        )


class RunReport(SecuredView):
    @openapi.metadata(
        description=(
            "A service worker reports the outcome of a run (the `report_url` its task carries); the job's kind "
            "applies it. Needs an API key with `tasks.report_job`. Idempotent; 409 when the job was cancelled."
        ),
        tags=["Jobs"],
        summary="Report a run's outcome",
    )
    def post(self, request, run_id: UUID):
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError as error:
            raise ProblemDetailException(_("The report must be JSON."), status=HTTPStatus.BAD_REQUEST, previous=error)
        if not isinstance(payload, dict):
            raise ProblemDetailException(_("The report must be a JSON object."), status=HTTPStatus.BAD_REQUEST)
        try:
            run = facade.report(run_id, payload, user=request.user)
        except DispatchError as error:
            raise _refused(error)
        return SingleResponse(request, data=RunSerializer.Detail.model_validate(run))
