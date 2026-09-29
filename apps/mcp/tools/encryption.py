"""Operations tools: inspect and retry Readium LCP encryption jobs.

An encryption job is not curation. It spends worker time, writes an encrypted
package to shared storage and registers it with the LCP server, so both tools
here are **restricted to administrators** — the same gate as
`POST /readium/v1/entries/{id}/encryption` (`core.change_entry`, which with
`default_permissions = ()` on `Entry` is a superuser check in all but name).
`manage` access on a catalog does not confer it.

The check runs before any lookup, so a non-administrator learns nothing about
which publications have jobs — the refusal is the same whatever ids are sent.

lcpencrypt reports back only on success. A job lost in transit (wrong broker,
worker without the storage mount) therefore stays `pending` forever and never
flips to `failed`; `list_encryption_jobs` is how an operator finds those, and
`requeue_encryption` sends them again.
"""

import logging

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
    },
    "required": ["content_id", "status", "entry_id"],
}


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
        "encrypted_at": encrypted_content.encrypted_at.isoformat() if encrypted_content.encrypted_at else None,
    }


def _jobs_queryset():
    return EncryptedContent.objects.select_related("acquisition__entry__catalog")


@registry.tool(
    name="list_encryption_jobs",
    title="List Readium encryption jobs",
    description=(
        "List Readium LCP encryption jobs, oldest first, so an operator can find the ones that are "
        "stuck. **Restricted to administrators.** By default returns jobs still `pending` or "
        "`failed` — a publication cannot be lent until its job is `registered`. A job that has sat "
        "in `pending` for more than a few minutes almost certainly never reached the worker "
        "(lcpencrypt only reports back on success, so a lost job never turns `failed`). Pass the "
        "`content_id` values to `requeue_encryption` to retry them."
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
            result.update(requeued=True, status=encrypted_content.status)
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
