"""Background jobs (IP-031): the same ledger and controls as `/api/v1/jobs`.

**Administrators only** (each kind's permission, `core.change_entry` by default — a superuser check
in practice). The check runs before any lookup. Every Celery task the catalog or a service worker
ran is a job; a job kind says what it is about (an acquisition, an entry) and what can be done with
it. `list_job_kinds` lists them.
"""

import logging

from django.utils.translation import gettext as _

from apps.api.filters.jobs import JobFilter
from apps.api.serializers.jobs import JobSerializer, job_detail
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolError, ToolNotFound
from apps.mcp.pagination import PAGINATION_ARGUMENTS, pagination_schema, paginate, read_pagination
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA, list_output, single_output
from apps.tasks.kinds import JobRefused, registry as kinds
from apps.tasks.models import JobProtocol
from apps.tasks.services import JobService

logger = logging.getLogger("apps.mcp.audit")

STATUSES = tuple(JobProtocol.JobStatus.values)

JOB_SCHEMA = {
    "type": "object",
    "description": "A background job. `detail` holds the kind's own fields (e.g. `encryption` for readium.encrypt).",
    "properties": {
        "id": UUID_SCHEMA,
        "kind": {"type": ["string", "null"]},
        "name": {"type": "string", "description": "Celery task name."},
        "status": {"type": "string", "enum": list(STATUSES)},
        "stale": {"type": "boolean"},
        "subject_type": {"type": ["string", "null"]},
        "subject_id": {"type": ["string", "null"]},
        "entry_id": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
    },
    "required": ["id", "name", "status"],
}


def _require(request):
    try:
        JobService.require(request.user)
    except JobRefused:
        raise ToolError(
            _("Background jobs are restricted to administrators. `manage` access on a catalog does not confer it.")
        )


def _raise(error: JobRefused):
    if error.status == 409:
        raise ToolConflict(error.message)
    raise ToolError(error.message)


def _job(job_id: str) -> JobProtocol:
    job = JobProtocol.objects.filter(pk=job_id).first()
    if job is None:
        raise ToolNotFound("Job", job_id)
    return job


def _detail(job) -> dict:
    return job_detail(job).model_dump(mode="json")


@registry.tool(
    name="list_job_kinds",
    title="List background job kinds",
    description=(
        "The kinds of background job the catalog knows: what each is about (`subject_type`), whether it can be "
        "started by hand, its options and its actions (e.g. readium.encrypt: requeue, reencrypt). "
        "**Administrators only.**"
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema={"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
    access=ToolAccess.USER,
)
def list_job_kinds(request, raw_arguments: dict) -> dict:
    Arguments(raw_arguments, allowed=())
    _require(request)
    return {"items": [kind.describe() for kind in kinds.all()]}


@registry.tool(
    name="list_jobs",
    title="List background jobs",
    description=(
        "List background jobs, newest first: LCP encryption, AI processing (extraction), OCR, table of contents, "
        "notifications, maintenance. **Administrators only.** Filter by `kind`, `status`, `stale` (queued too "
        "long: almost certainly lost), `entry_id`, `catalog_id`. `latest: true` keeps the newest job per kind and "
        "subject — the current state of each publication. Then `get_job` for events and errors, `retry_job`, "
        "`revoke_job` or `run_job_action`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "array", "items": {"type": "string"}, "description": "Job kinds (`list_job_kinds`)."},
            "status": {"type": "array", "items": {"type": "string", "enum": list(STATUSES)}},
            "stale": {"type": "boolean"},
            "latest": {"type": "boolean"},
            "entry_id": UUID_SCHEMA,
            "catalog_id": UUID_SCHEMA,
            "subject_id": UUID_SCHEMA,
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(JOB_SCHEMA),
    access=ToolAccess.USER,
)
def list_jobs(request, raw_arguments: dict) -> dict:
    arguments = Arguments(
        raw_arguments,
        allowed=("kind", "status", "stale", "latest", "entry_id", "catalog_id", "subject_id", *PAGINATION_ARGUMENTS),
    )
    _require(request)
    requested = read_pagination(arguments)
    params = {
        "kind": arguments.string_list("kind"),
        "status": arguments.enum_list("status", STATUSES),
        "entry_id": arguments.uuid("entry_id"),
        "catalog_id": arguments.uuid("catalog_id"),
        "subject_id": arguments.uuid("subject_id"),
    }
    for flag in ("stale", "latest"):
        value = arguments.boolean(flag)
        if value is not None:
            params[flag] = "true" if value else "false"
    params = {key: value for key, value in params.items() if value is not None}
    queryset = JobFilter(params, queryset=JobProtocol.objects.all(), request=request).qs.order_by("-created_at")
    page = paginate(queryset, requested)
    return {
        "items": [JobSerializer.Base.model_validate(job).model_dump(mode="json") for job in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="get_job",
    title="Get a background job",
    description=(
        "One job with its Celery events, error and traceback, its follow-up tasks (`children`), its retries and "
        "the kind's own fields (`detail`). **Administrators only.**"
    ),
    input_schema={
        "type": "object",
        "properties": {"job_id": UUID_SCHEMA},
        "required": ["job_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("job", JOB_SCHEMA),
    access=ToolAccess.USER,
)
def get_job(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("job_id",))
    _require(request)
    return {"job": _detail(_job(arguments.uuid("job_id", required=True)))}


@registry.tool(
    name="start_job",
    title="Start a background job",
    description=(
        "Start a job of `kind` on `subject_id` (an acquisition for files.ocr, readium.encrypt, extraction.run; an "
        "entry for core.table_of_contents; none for maintenance.*). **Administrators only.** Refused while a job of "
        "the kind is active for the subject unless `force`. Starting work spends worker time: confirm with the user."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "subject_id": UUID_SCHEMA,
            "options": {"type": "object"},
            "force": {"type": "boolean", "default": False},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    output_schema=single_output("job", JOB_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
def start_job(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("kind", "subject_id", "options", "force"))
    _require(request)
    options = raw_arguments.get("options") or {}
    if not isinstance(options, dict):
        raise ToolError(_("`options` must be an object."))
    try:
        job = JobService.start(
            arguments.string("kind", max_length=100),
            arguments.uuid("subject_id"),
            options=options,
            user=request.user,
            force=bool(arguments.boolean("force", default=False)),
        )
    except JobRefused as error:
        _raise(error)
    logger.info("mcp.write user=%s action=start_job kind=%s job=%s", request.user.pk, job.kind, job.pk)
    return {"job": _detail(job)}


@registry.tool(
    name="retry_job",
    title="Retry a background job",
    description=(
        "Run a finished job again as a new job. **Administrators only.** Kinds start again with the same options; "
        "a task without a kind is re-sent with its recorded arguments when they were recorded exactly."
    ),
    input_schema={
        "type": "object",
        "properties": {"job_id": UUID_SCHEMA},
        "required": ["job_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("job", JOB_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
def retry_job(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("job_id",))
    _require(request)
    try:
        job = JobService.retry(_job(arguments.uuid("job_id", required=True)), user=request.user)
    except JobRefused as error:
        _raise(error)
    logger.info("mcp.write user=%s action=retry_job job=%s", request.user.pk, job.pk)
    return {"job": _detail(job)}


@registry.tool(
    name="revoke_job",
    title="Revoke a background job",
    description=(
        "Stop a job that has not started yet; no worker will run it. **Administrators only.** A running job stops "
        "only with `terminate`, and only for kinds that allow it (workers that write files do not)."
    ),
    input_schema={
        "type": "object",
        "properties": {"job_id": UUID_SCHEMA, "terminate": {"type": "boolean", "default": False}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("job", JOB_SCHEMA),
    access=ToolAccess.WRITE,
    destructive=True,
)
def revoke_job(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("job_id", "terminate"))
    _require(request)
    try:
        job = JobService.revoke(
            _job(arguments.uuid("job_id", required=True)),
            user=request.user,
            terminate=bool(arguments.boolean("terminate", default=False)),
        )
    except JobRefused as error:
        _raise(error)
    logger.info("mcp.write user=%s action=revoke_job job=%s", request.user.pk, job.pk)
    return {"job": _detail(job)}


@registry.tool(
    name="run_job_action",
    title="Run a background job action",
    description=(
        "Run a kind-specific action on a job (see `list_job_kinds`): readium.encrypt `requeue` (send a stuck or "
        "failed encryption again) or `reencrypt` (options `dry_run`, `force`; active loans break, so dry_run first "
        "and confirm `force` with the user). **Administrators only.**"
    ),
    input_schema={
        "type": "object",
        "properties": {"job_id": UUID_SCHEMA, "action": {"type": "string"}, "options": {"type": "object"}},
        "required": ["job_id", "action"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"result": {"type": "object"}}, "required": ["result"]},
    access=ToolAccess.WRITE,
    idempotent=False,
)
def run_job_action(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("job_id", "action", "options"))
    _require(request)
    options = raw_arguments.get("options") or {}
    if not isinstance(options, dict):
        raise ToolError(_("`options` must be an object."))
    action = arguments.string("action", max_length=50)
    try:
        result = JobService.run_action(
            _job(arguments.uuid("job_id", required=True)), action, user=request.user, options=options
        )
    except JobRefused as error:
        _raise(error)
    return {"result": result}
