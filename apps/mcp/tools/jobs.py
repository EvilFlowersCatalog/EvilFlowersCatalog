"""Background jobs (IP-031 rev. 2): the same declarative facade as `/api/v1/jobs`.

**Administrators only.** A job is declared, not commanded: `apply_job` states the spec and the desired
state of a kind's job on a subject; the catalog's reconciler runs it, blocks it (with a reason) until it
can, or cancels it. Retry = apply again; cancel = `desired_state: cancelled`.
"""

import logging

from django.utils.translation import gettext as _

from apps.api.filters.jobs import JobFilter
from apps.api.serializers.jobs import JobSerializer
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolError, ToolNotFound
from apps.mcp.pagination import PAGINATION_ARGUMENTS, pagination_schema, paginate, read_pagination
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA, list_output, single_output
from apps.tasks.application.facade import Plan, facade
from apps.tasks.application.registry import registry as definitions
from apps.tasks.contract import DispatchError
from apps.tasks.domain.values import JobState

logger = logging.getLogger("apps.mcp.audit")

STATES = tuple(state.value for state in JobState)

JOB_SCHEMA = {
    "type": "object",
    "description": "A job: declared `spec`/`desired_state`, observed `state`/`reason`/`detail`.",
    "properties": {
        "id": UUID_SCHEMA,
        "kind": {"type": "string"},
        "state": {"type": "string", "enum": list(STATES)},
        "desired_state": {"type": "string", "enum": ["completed", "cancelled"]},
        "reason": {"type": ["string", "null"]},
        "subject_id": {"type": ["string", "null"]},
        "entry_id": {"type": ["string", "null"]},
        "stale": {"type": "boolean"},
    },
    "required": ["id", "kind", "state"],
}


def _require(request):
    try:
        facade.require(request.user)
    except DispatchError:
        raise ToolError(
            _("Background jobs are restricted to administrators. `manage` access on a catalog does not confer it.")
        )


def _raise(error: DispatchError):
    if error.status == 404:
        raise ToolNotFound("Job", error.message)
    if error.status == 409:
        raise ToolConflict(error.message)
    raise ToolError(error.message)


@registry.tool(
    name="list_job_kinds",
    title="List background job kinds",
    description=(
        "The kinds of background job: what each is about (`subject_type`), its spec schema, whether it reports or "
        "is run-only (recorded, never declared). **Administrators only.**"
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema={"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
    access=ToolAccess.USER,
)
def list_job_kinds(request, raw_arguments: dict) -> dict:
    Arguments(raw_arguments, allowed=())
    _require(request)
    return {"items": [definition.describe() for definition in definitions.all()]}


@registry.tool(
    name="list_jobs",
    title="List background jobs",
    description=(
        "List jobs (one per kind and subject): LCP encryption, AI processing, OCR, table of contents, maintenance. "
        "**Administrators only.** Filter by `kind`, `state` (e.g. failed, blocked), `stale` (queued too long), "
        "`entry_id`, `catalog_id`. Then `get_job` for runs and transitions, `apply_job` to retry or cancel."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "array", "items": {"type": "string"}},
            "state": {"type": "array", "items": {"type": "string", "enum": list(STATES)}},
            "stale": {"type": "boolean"},
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
        allowed=("kind", "state", "stale", "entry_id", "catalog_id", "subject_id", *PAGINATION_ARGUMENTS),
    )
    _require(request)
    requested = read_pagination(arguments)
    params = {
        "kind": arguments.string_list("kind"),
        "state": arguments.enum_list("state", STATES),
        "entry_id": arguments.uuid("entry_id"),
        "catalog_id": arguments.uuid("catalog_id"),
        "subject_id": arguments.uuid("subject_id"),
    }
    stale = arguments.boolean("stale")
    if stale is not None:
        params["stale"] = "true" if stale else "false"
    params = {key: value for key, value in params.items() if value is not None}
    queryset = JobFilter(params, queryset=facade.jobs(), request=request).qs.order_by("-last_transition_at")
    page = paginate(queryset, requested)
    return {
        "items": [JobSerializer.Base.model_validate(job).model_dump(mode="json") for job in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="get_job",
    title="Get a background job",
    description="One job with its runs, run counts per task, transitions and the jobs it declared. **Administrators only.**",
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
    try:
        job = facade.get(arguments.uuid("job_id", required=True))
    except DispatchError as error:
        _raise(error)
    return {"job": JobSerializer.Detail.from_job(job).model_dump(mode="json")}


@registry.tool(
    name="apply_job",
    title="Apply a background job",
    description=(
        "Declare a job: the `spec` and `desired_state` of a kind's job on `subject_id` (an acquisition for files.ocr, "
        "readium.encrypt, extraction.run; an entry for core.table_of_contents; none for maintenance.*). The catalog "
        "runs it, or blocks it with a `reason` until it can (LCP off, active loans …). Applying the same spec to a "
        "job still on its way changes nothing; to a finished job it runs again (retry). `desired_state: cancelled` "
        "stops it. `dry_run: true` reports what would happen. E.g. re-encrypt: kind readium.encrypt, spec "
        "{reencrypt: true}; add force only after confirming with the user (active loans break). "
        "**Administrators only.** Spends worker time: confirm with the user."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "subject_id": UUID_SCHEMA,
            "spec": {"type": "object"},
            "desired_state": {"type": "string", "enum": ["completed", "cancelled"], "default": "completed"},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"job": JOB_SCHEMA, "plan": {"type": "object"}}},
    access=ToolAccess.WRITE,
    idempotent=True,
)
def apply_job(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("kind", "subject_id", "spec", "desired_state", "dry_run"))
    _require(request)
    spec = raw_arguments.get("spec") or {}
    if not isinstance(spec, dict):
        raise ToolError(_("`spec` must be an object."))
    dry_run = bool(arguments.boolean("dry_run", default=False))
    try:
        result = facade.apply(
            arguments.string("kind", max_length=100),
            arguments.uuid("subject_id"),
            spec=spec,
            desired=arguments.enum("desired_state", ("completed", "cancelled"), default="completed"),
            user=request.user,
            dry_run=dry_run,
        )
    except DispatchError as error:
        _raise(error)
    if isinstance(result, Plan):
        return {"plan": JobSerializer.Plan.from_plan(result).model_dump(mode="json")}
    logger.info("mcp.write user=%s action=apply_job kind=%s job=%s", request.user.pk, result.kind, result.pk)
    return {"job": JobSerializer.Base.model_validate(result).model_dump(mode="json")}
