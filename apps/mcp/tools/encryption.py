"""Operations tools: inspect, retry and re-run Readium LCP encryption jobs.

An encryption job is not curation. It spends worker time, writes an encrypted
package to shared storage and registers it with the LCP server, so every tool
here is **restricted to administrators** — the same gate as
`POST /readium/v1/entries/{id}/encryption` (`core.change_entry`, which with
`default_permissions = ()` on `Entry` is a superuser check in all but name).
`manage` access on a catalog does not confer it.

The check runs before any lookup, so a non-administrator learns nothing about
which publications have jobs — the refusal is the same whatever ids are sent.

With `EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL` set, the worker reports every
task's outcome and a failure in the worker turns the job `failed` with its
error (issue #99). A job that never reached a worker (wrong broker, worker
down) still sends nothing and stays `pending`; `list_encryption_jobs` flags it
`stale` once it has waited longer than
`EVILFLOWERS_READIUM_ENCRYPTION_STALE_MINUTES`, and `requeue_encryption` sends
it again. Each job carries the Celery `task_id` and the `trace_id` of its last
dispatch, so its run can be found in Tempo.

`reencrypt_publication` re-runs a job that already succeeded (issue #98), e.g.
after a worker upgrade, through `ContentEncryptionService.reencrypt`: same
content id, active-loan guard unless `force`, `dry_run` to look first.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolError, ToolNotFound
from apps.mcp.pagination import PAGINATION_ARGUMENTS, pagination_schema, paginate, read_pagination
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA, list_output
from apps.mcp.tools.common import assert_within_bulk_limit
from apps.readium.models import EncryptedContent
from apps.readium.services.content_encryption_service import ContentEncryptionService

logger = logging.getLogger("apps.mcp.audit")

ENCRYPTION_STATUSES = tuple(EncryptedContent.EncryptionStatus.values)

ENCRYPTION_JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "content_id": {"type": "string", "description": "LCP content id; pass it to `requeue_encryption`."},
        "status": {"type": "string", "enum": list(ENCRYPTION_STATUSES)},
        "entry_id": UUID_SCHEMA,
        "entry_title": {"type": "string"},
        "catalog": {"type": "string", "description": "Catalog `url_name`."},
        "mime": {"type": "string"},
        "input_path": {"type": "string", "description": "Source file, relative to the storage root."},
        "encrypted_path": {"type": "string", "description": "Output package, relative to the storage root."},
        "error_message": {"type": ["string", "null"]},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "encrypted_at": {"type": ["string", "null"], "format": "date-time"},
        "task_id": {"type": ["string", "null"], "description": "Celery task id of the last dispatch."},
        "trace_id": {
            "type": ["string", "null"],
            "description": "Trace of the last dispatch; it continues into the worker run and its result report.",
        },
        "attempts": {"type": "integer", "description": "How many times the job was sent to the worker."},
        "last_dispatched_at": {"type": ["string", "null"], "format": "date-time"},
        "reported_at": {
            "type": ["string", "null"],
            "format": "date-time",
            "description": "When the worker reported the outcome of the last dispatch.",
        },
        "report": {
            "type": ["object", "null"],
            "description": "The worker's report of the last dispatch: cover, pages, toc_entries, warnings, "
            "protected_content_* (on success).",
        },
        "stale": {
            "type": "boolean",
            "description": "`pending`, no report, and dispatched longer ago than the stale threshold: "
            "almost certainly lost.",
        },
    },
    "required": ["content_id", "status", "entry_id"],
}


def _isoformat(value):
    return value.isoformat() if value else None


def _is_stale(encrypted_content: EncryptedContent) -> bool:
    if encrypted_content.status != EncryptedContent.EncryptionStatus.PENDING or encrypted_content.reported_at:
        return False
    # Rows dispatched before #99 have no `last_dispatched_at`.
    dispatched_at = encrypted_content.last_dispatched_at or encrypted_content.updated_at
    threshold = timedelta(minutes=settings.EVILFLOWERS_READIUM_ENCRYPTION_STALE_MINUTES)
    return timezone.now() - dispatched_at > threshold


def _require_administrator(request) -> None:
    if not request.user.has_perm("core.change_entry"):
        raise ToolError(
            _(
                "Encryption jobs are restricted to administrators. `manage` access on a catalog "
                "does not confer it — ask an administrator to inspect or re-queue the job."
            )
        )


def _job(encrypted_content: EncryptedContent) -> dict:
    acquisition = encrypted_content.acquisition
    entry = acquisition.entry
    return {
        "content_id": encrypted_content.lcp_content_id,
        "status": encrypted_content.status,
        "entry_id": str(entry.pk),
        "entry_title": entry.title,
        "catalog": entry.catalog.url_name,
        "mime": acquisition.mime,
        "input_path": acquisition.content.name if acquisition.content else "",
        "encrypted_path": encrypted_content.encrypted_path,
        "error_message": encrypted_content.error_message,
        "created_at": encrypted_content.created_at.isoformat(),
        "updated_at": encrypted_content.updated_at.isoformat(),
        "encrypted_at": _isoformat(encrypted_content.encrypted_at),
        "task_id": encrypted_content.task_id,
        "trace_id": encrypted_content.trace_id,
        "attempts": encrypted_content.attempts,
        "last_dispatched_at": _isoformat(encrypted_content.last_dispatched_at),
        "reported_at": _isoformat(encrypted_content.reported_at),
        "report": encrypted_content.report,
        "stale": _is_stale(encrypted_content),
    }


def _jobs_queryset():
    return EncryptedContent.objects.select_related("acquisition__entry__catalog")


@registry.tool(
    name="list_encryption_jobs",
    title="List Readium encryption jobs",
    description=(
        "List Readium LCP encryption jobs, oldest first, so an operator can find the ones that are "
        "stuck. **Restricted to administrators.** By default returns jobs still `pending` or "
        "`failed` — a publication cannot be lent until its job is `registered`. The worker reports "
        "failures, so a job it could not encrypt turns `failed` with the worker's `error_message`. "
        "A job marked `stale` sat in `pending` without any report and almost certainly never "
        "reached a worker. Each job carries `task_id` and `trace_id` of its last dispatch (look the "
        "trace up in Tempo) and the worker's `report`. Pass the `content_id` values to "
        "`requeue_encryption` to retry them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "array",
                "items": {"type": "string", "enum": list(ENCRYPTION_STATUSES)},
                "description": "Statuses to include. Defaults to ['pending', 'failed'].",
            },
            "catalog_id": {**UUID_SCHEMA, "description": "Only jobs for publications in this catalog."},
            "entry_ids": {
                "type": "array",
                "items": UUID_SCHEMA,
                "description": "Only jobs for these publications — e.g. the ones just uploaded.",
            },
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(ENCRYPTION_JOB_SCHEMA),
    access=ToolAccess.USER,
)
def list_encryption_jobs(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("status", "catalog_id", "entry_ids", *PAGINATION_ARGUMENTS))
    _require_administrator(request)

    requested = read_pagination(arguments)
    statuses = arguments.enum_list("status", ENCRYPTION_STATUSES)
    catalog_id = arguments.uuid("catalog_id")
    entry_ids = arguments.uuid_sequence("entry_ids")

    queryset = _jobs_queryset().filter(
        status__in=statuses.split(",") if statuses else ContentEncryptionService.REQUEUEABLE_STATUSES
    )
    if catalog_id:
        queryset = queryset.filter(acquisition__entry__catalog_id=catalog_id)
    if entry_ids:
        queryset = queryset.filter(acquisition__entry_id__in=entry_ids)

    page = paginate(queryset.order_by("created_at"), requested)
    return {"items": [_job(ec) for ec in page.items], "metadata": page.metadata()}


@registry.tool(
    name="requeue_encryption",
    title="Re-queue Readium encryption jobs",
    description=(
        "Send stuck or failed Readium LCP encryption jobs to the encryption worker again. "
        "**Restricted to administrators.** Takes `content_id` values from `list_encryption_jobs`. "
        "Only `pending` and `failed` jobs are re-sent; `completed` and `registered` ones are "
        "reported as skipped and never re-encrypted. Each job keeps its content id and output "
        "path and goes back to `pending` until the worker's webhook marks it `registered`. "
        "\n\n"
        "Re-queuing does not fix whatever lost the job in the first place — if it was lost "
        "because of the deployment (broker, storage mount), fix that first or it will be lost "
        "again. Calling this twice sends the job twice."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content_ids": {
                "type": "array",
                "items": UUID_SCHEMA,
                "description": "LCP content ids from `list_encryption_jobs`.",
            },
        },
        "required": ["content_ids"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "requeued": {"type": "integer"},
            "skipped": {"type": "integer"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content_id": {"type": "string"},
                        "entry_title": {"type": "string"},
                        "requeued": {"type": "boolean"},
                        "status": {"type": "string"},
                        "task_id": {"type": ["string", "null"], "description": "Celery task id of the new dispatch."},
                        "trace_id": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["content_id", "requeued", "status"],
                },
            },
        },
        "required": ["requeued", "skipped", "results"],
    },
    access=ToolAccess.WRITE,
    # Every call dispatches another worker task.
    idempotent=False,
)
def requeue_encryption(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("content_ids",))
    _require_administrator(request)

    content_ids = assert_within_bulk_limit("content_ids", arguments.uuid_sequence("content_ids") or [])

    jobs = {ec.lcp_content_id: ec for ec in _jobs_queryset().filter(lcp_content_id__in=content_ids)}
    missing = [content_id for content_id in content_ids if content_id not in jobs]
    if missing:
        raise ToolNotFound("Encryption job", ", ".join(missing[:10]))

    results = []
    for content_id in content_ids:
        encrypted_content = jobs[content_id]
        result = {"content_id": content_id, "entry_title": encrypted_content.acquisition.entry.title}
        try:
            ContentEncryptionService.requeue(encrypted_content)
        except ValueError as error:
            result.update(requeued=False, status=encrypted_content.status, reason=str(error))
        else:
            result.update(
                requeued=True,
                status=encrypted_content.status,
                task_id=encrypted_content.task_id,
                trace_id=encrypted_content.trace_id,
            )
        results.append(result)

    requeued = sum(1 for result in results if result["requeued"])
    logger.info(
        "mcp.write user=%s action=requeue_encryption requested=%s requeued=%s content_ids=%s",
        request.user.pk,
        len(content_ids),
        requeued,
        ",".join(content_ids),
    )

    return {"requeued": requeued, "skipped": len(results) - requeued, "results": results}


@registry.tool(
    name="reencrypt_publication",
    title="Re-encrypt Readium publications",
    description=(
        "Encrypt a publication's Readium LCP package again, e.g. after the encryption worker was "
        "upgraded (cover, page count, table of contents, full metadata). **Restricted to "
        "administrators.** Unlike `requeue_encryption` it also acts on `registered` and `completed` "
        "jobs. Pass `content_ids` from `list_encryption_jobs` or `entry_ids`, not both. Each job keeps "
        "its content id (the LCP server updates the stored content in place) and goes back to "
        "`pending`; the publication cannot be lent until the worker's webhook marks it `registered` "
        "again. Each result returns the Celery `task_id` and `trace_id` of the new dispatch."
        "\n\n"
        "Loans issued before the re-encryption carry the old content key: a reader's copy stops "
        "opening once it refreshes the license or downloads the publication again. A job with "
        "`ready` or `active` loans is therefore skipped, with the count in `reason`, unless `force` "
        "is set — confirm with the user before forcing. Run with `dry_run: true` first: it reports "
        "per job the loans that would break and the metadata that would be sent, and dispatches "
        "nothing. Calling this twice sends the job twice."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content_ids": {
                "type": "array",
                "items": UUID_SCHEMA,
                "description": "LCP content ids from `list_encryption_jobs`.",
            },
            "entry_ids": {
                "type": "array",
                "items": UUID_SCHEMA,
                "description": "Publications whose encryption jobs to re-run (all jobs of each).",
            },
            "force": {
                "type": "boolean",
                "default": False,
                "description": "Re-encrypt even with `ready`/`active` loans. Their readers lose access.",
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "Report what would be re-sent and which jobs have loans; dispatch nothing.",
            },
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "dry_run": {"type": "boolean"},
            "requeued": {"type": "integer", "description": "Jobs dispatched (would be, in a dry run)."},
            "skipped": {"type": "integer"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content_id": {"type": "string"},
                        "entry_id": UUID_SCHEMA,
                        "entry_title": {"type": "string"},
                        "previous_status": {"type": "string", "enum": list(ENCRYPTION_STATUSES)},
                        "requeued": {
                            "type": "boolean",
                            "description": "Dispatched; in a dry run, whether it would be.",
                        },
                        "status": {"type": "string", "enum": list(ENCRYPTION_STATUSES)},
                        "active_loans": {"type": "integer", "description": "Loans in `ready`/`active` state."},
                        "task_id": {"type": ["string", "null"], "description": "Celery task id of the new dispatch."},
                        "trace_id": {"type": ["string", "null"]},
                        "metadata": {
                            "type": "object",
                            "description": "Dry run only: the publication metadata the worker would receive.",
                        },
                        "reason": {"type": "string", "description": "Why the job was (or would be) skipped."},
                    },
                    "required": ["content_id", "entry_id", "requeued", "status", "active_loans"],
                },
            },
        },
        "required": ["dry_run", "requeued", "skipped", "results"],
    },
    access=ToolAccess.WRITE,
    # Every call dispatches another worker task.
    idempotent=False,
)
def reencrypt_publication(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("content_ids", "entry_ids", "force", "dry_run"))
    _require_administrator(request)

    content_ids = arguments.uuid_sequence("content_ids")
    entry_ids = arguments.uuid_sequence("entry_ids")
    force = arguments.boolean("force", default=False)
    dry_run = arguments.boolean("dry_run", default=False)

    if bool(content_ids) == bool(entry_ids):
        raise ToolError(_("Pass either `content_ids` or `entry_ids` (one of them, not both)."))

    if content_ids:
        content_ids = assert_within_bulk_limit("content_ids", content_ids)
        found = {ec.lcp_content_id: ec for ec in _jobs_queryset().filter(lcp_content_id__in=content_ids)}
        missing = [content_id for content_id in content_ids if content_id not in found]
        if missing:
            raise ToolNotFound("Encryption job", ", ".join(missing[:10]))
        jobs = [found[content_id] for content_id in content_ids]
    else:
        entry_ids = assert_within_bulk_limit("entry_ids", entry_ids)
        jobs = list(_jobs_queryset().filter(acquisition__entry_id__in=entry_ids).order_by("created_at"))
        with_jobs = {str(ec.acquisition.entry_id) for ec in jobs}
        missing = [entry_id for entry_id in entry_ids if entry_id not in with_jobs]
        if missing:
            raise ToolNotFound("Encryption job for publication", ", ".join(missing[:10]))
        # An entry can hold several packages (one per PDF/EPUB acquisition).
        assert_within_bulk_limit("entry_ids", jobs)

    results = []
    for encrypted_content in jobs:
        entry = encrypted_content.acquisition.entry
        active_loans = ContentEncryptionService.active_licenses(encrypted_content).count()
        result = {
            "content_id": encrypted_content.lcp_content_id,
            "entry_id": str(entry.pk),
            "entry_title": entry.title,
            "previous_status": encrypted_content.status,
            "active_loans": active_loans,
        }

        reason = None
        if not entry.read_config("readium_enabled"):
            reason = _("Readium LCP is switched off for this publication.")
        elif not encrypted_content.acquisition.content:
            reason = _("The publication has no file to encrypt.")
        elif active_loans and not force:
            reason = _(
                "%(count)d active loan(s) would break; their readers lose access. Pass `force` to "
                "re-encrypt anyway."
            ) % {"count": active_loans}

        if reason:
            result.update(requeued=False, status=encrypted_content.status, reason=reason)
        elif dry_run:
            result.update(
                requeued=True,
                status=encrypted_content.status,
                metadata=ContentEncryptionService.publication_metadata(entry),
            )
        else:
            try:
                ContentEncryptionService.reencrypt(encrypted_content, force=force)
            except ValueError as error:
                # A loan granted since `active_loans` was counted.
                result.update(requeued=False, status=encrypted_content.status, reason=str(error))
                results.append(result)
                continue
            result.update(
                requeued=True,
                status=encrypted_content.status,
                task_id=encrypted_content.task_id,
                trace_id=encrypted_content.trace_id,
            )
            # `_queue_encryption_task` turns the job `failed` when it cannot
            # prepare the source (license page); nothing reached the worker,
            # so the row's task id is still the previous dispatch's.
            if encrypted_content.status == EncryptedContent.EncryptionStatus.FAILED:
                result.update(
                    requeued=False, task_id=None, trace_id=None, reason=encrypted_content.error_message or ""
                )
        results.append(result)

    requeued = sum(1 for result in results if result["requeued"])
    if not dry_run:
        logger.info(
            "mcp.write user=%s action=reencrypt_publication requested=%s requeued=%s force=%s content_ids=%s",
            request.user.pk,
            len(jobs),
            requeued,
            force,
            ",".join(ec.lcp_content_id for ec in jobs),
        )

    return {"dry_run": dry_run, "requeued": requeued, "skipped": len(results) - requeued, "results": results}
