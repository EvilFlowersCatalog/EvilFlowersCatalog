"""Harvest sources (IP-034): look at what a source matched, and start a run.

A source belongs to its target catalog: these tools need `manage` there (or administrator rights), like the
REST admin API. `run_harvest` declares the `harvest.run` job itself after that check, so a catalog manager can
start a run without the administrator-only `apply_job`.
"""

import logging

from django.db.models import Q

from apps.harvest.filters import manageable_sources
from apps.harvest.models import HarvestRecord, HarvestSource
from apps.harvest.serializers import HarvestRecordSerializer, HarvestSourceSerializer
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolError, ToolNotFound
from apps.mcp.pagination import PAGINATION_ARGUMENTS, pagination_schema, paginate, read_pagination
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import UUID_SCHEMA, list_output, single_output
from apps.tasks.contract import DispatchError, declare

logger = logging.getLogger("apps.mcp.audit")

STATUSES = tuple(HarvestRecord.Status.values)

SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "slug": {"type": "string"},
        "name": {"type": "string"},
        "adapter": {"type": "string", "description": "A registered connector, e.g. oai_pmh, marc_file."},
        "config": {"type": "object"},
        "base_url": {"type": ["string", "null"]},
        "metadata_prefix": {"type": "string"},
        "set_spec": {"type": ["string", "null"]},
        "system_key": {"type": "string"},
        "target_catalog_id": UUID_SCHEMA,
        "mode": {"type": "string", "enum": list(HarvestSource.Mode.values)},
        "field_policy": {"type": "object"},
        "schedule": {"type": "string"},
        "enabled": {"type": "boolean"},
        "cursor": {"type": ["string", "null"]},
        "has_upload": {"type": "boolean"},
        "last_run_at": {"type": ["string", "null"]},
        "last_report": {"type": ["object", "null"]},
        "record_counts": {"type": "object", "description": "Mirror records of real runs per status."},
        "dry_run_counts": {"type": "object", "description": "Records of the last dry run per status."},
    },
    "required": ["id", "slug", "adapter", "mode", "target_catalog_id"],
}

RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "record_id": {"type": "string"},
        "status": {"type": "string", "enum": list(STATUSES)},
        "matched_on": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
        "entry_id": {"type": ["string", "null"]},
        "dry_run": {"type": "boolean"},
        "deleted": {"type": "boolean"},
        "title": {"type": ["string", "null"]},
        "identifiers": {"type": "object"},
    },
    "required": ["id", "record_id", "status"],
}


def _source(request, source_id: str) -> HarvestSource:
    source = manageable_sources(request.user).filter(pk=source_id).first()
    if source is None:
        raise ToolNotFound("Harvest source", source_id)
    return source


@registry.tool(
    name="list_harvest_sources",
    title="List harvest sources",
    description=(
        "Sources the catalog harvests metadata from (OAI-PMH catalogues, uploaded MARC files) into a target "
        "catalog, with their mode (`link_only`, `enrich`, `create`), schedule and the last run's counters. Only "
        "sources of catalogs you manage are listed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Only sources of this catalog."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(SOURCE_SCHEMA),
    access=ToolAccess.USER,
)
def list_harvest_sources(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("catalog_id", *PAGINATION_ARGUMENTS))
    requested = read_pagination(arguments)
    queryset = manageable_sources(request.user).order_by("slug")
    catalog_id = arguments.uuid("catalog_id")
    if catalog_id:
        queryset = queryset.filter(target_catalog_id=catalog_id)
    page = paginate(queryset, requested)
    return {
        "items": [HarvestSourceSerializer.Base.model_validate(item).model_dump(mode="json") for item in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="get_harvest_source",
    title="Get a harvest source",
    description=(
        "One harvest source with its record counts per status: `matched`/`created`/`enriched` are linked to an "
        "entry, `unmatched` found none, `ambiguous` found several (see `list_harvest_records` for why)."
    ),
    input_schema={
        "type": "object",
        "properties": {"source_id": UUID_SCHEMA},
        "required": ["source_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("source", SOURCE_SCHEMA),
    access=ToolAccess.USER,
)
def get_harvest_source(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("source_id",))
    source = _source(request, arguments.uuid("source_id", required=True))
    return {"source": HarvestSourceSerializer.Detailed.of(source).model_dump(mode="json")}


@registry.tool(
    name="list_harvest_records",
    title="List harvest records",
    description=(
        "What a source's records said and what the matcher decided for each: status, the identifier it matched "
        "on, the entry, and a message for ambiguous or failed ones. Filter by `status`, `dry_run` (rows of the "
        "last dry run) and `query` (record id or title)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source_id": UUID_SCHEMA,
            "status": {"type": "array", "items": {"type": "string", "enum": list(STATUSES)}},
            "dry_run": {"type": "boolean", "default": False},
            "query": {"type": "string"},
            **pagination_schema(),
        },
        "required": ["source_id"],
        "additionalProperties": False,
    },
    output_schema=list_output(RECORD_SCHEMA),
    access=ToolAccess.USER,
)
def list_harvest_records(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("source_id", "status", "dry_run", "query", *PAGINATION_ARGUMENTS))
    requested = read_pagination(arguments)
    source = _source(request, arguments.uuid("source_id", required=True))
    queryset = HarvestRecord.objects.filter(source=source, dry_run=bool(arguments.boolean("dry_run", default=False)))
    statuses = arguments.enum_list("status", STATUSES)
    if statuses:
        queryset = queryset.filter(status__in=statuses.split(","))
    query = arguments.string("query", max_length=255)
    if query:
        queryset = queryset.filter(Q(record_id__icontains=query) | Q(source_record__title__icontains=query))
    page = paginate(queryset.order_by("record_id"), requested)
    items = []
    for row in page.items:
        data = HarvestRecordSerializer.Base.model_validate(row).model_dump(mode="json")
        record = data.pop("source_record") or {}
        data["title"] = record.get("title")
        data["identifiers"] = record.get("identifiers") or {}
        items.append(data)
    return {"items": items, "metadata": page.metadata()}


@registry.tool(
    name="run_harvest",
    title="Run a harvest",
    description=(
        "Start a run of a harvest source (a `harvest.run` background job). Requires `manage` on the source's "
        "catalog. Run `dry_run: true` first: it matches and reports (`list_harvest_records` with `dry_run: "
        "true`) without writing entries. A real run needs the source enabled and links, enriches or creates "
        "entries by its mode. `full: true` re-reads the whole source. Progress: `get_harvest_source`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source_id": UUID_SCHEMA,
            "dry_run": {"type": "boolean", "default": True},
            "full": {"type": "boolean", "default": False},
        },
        "required": ["source_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "job_id": UUID_SCHEMA,
            "state": {"type": "string"},
            "reason": {"type": ["string", "null"]},
            "spec": {"type": "object"},
        },
        "required": ["job_id", "state"],
    },
    access=ToolAccess.WRITE,
    idempotent=False,
)
def run_harvest(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("source_id", "dry_run", "full"))
    source = _source(request, arguments.uuid("source_id", required=True))
    spec = {
        "dry_run": bool(arguments.boolean("dry_run", default=True)),
        "full": bool(arguments.boolean("full", default=False)),
    }
    try:
        # The catalog check above is the permission: the jobs facade's own one is administrators only.
        job = declare("harvest.run", source.pk, spec=spec)
    except DispatchError as error:
        if error.status == 409:
            raise ToolConflict(error.message)
        raise ToolError(error.message)
    logger.info(
        "mcp.write user=%s action=run_harvest source=%s job=%s spec=%s", request.user.pk, source.slug, job.pk, spec
    )
    return {"job_id": str(job.pk), "state": job.state, "reason": job.reason, "spec": spec}


ENTRY_SYNC_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": UUID_SCHEMA,
        "slug": {"type": "string"},
        "name": {"type": "string"},
        "adapter": {"type": "string"},
        "mode": {"type": "string"},
        "record_id": {"type": "string"},
        "record_url": {"type": ["string", "null"]},
        "sync_state": {"type": ["string", "null"]},
        "last_synced_at": {"type": ["string", "null"]},
        "remote_version": {"type": ["string", "null"]},
        "local_version": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
        "open_conflicts": {"type": "integer"},
    },
    "required": ["source_id", "slug", "record_id"],
}


def _manageable_entry(request, entry_id: str):
    from object_checker.base_object_checker import has_object_permission

    from apps.core.models import Entry
    from apps.mcp.errors import ToolPermissionDenied

    entry = Entry.objects.select_related("catalog").filter(pk=entry_id).first()
    if entry is None or not has_object_permission("check_catalog_read", request.user, entry.catalog):
        raise ToolNotFound("Entry", entry_id)
    if not has_object_permission("check_catalog_manage", request.user, entry.catalog):
        raise ToolPermissionDenied("see the sync of", f"publication '{entry.title}'")
    return entry


@registry.tool(
    name="get_entry_sync",
    title="Get a publication's linked records",
    description=(
        "The external records a publication is linked to (library catalogues, repositories), with their URLs, and "
        "for two-way `sync` sources the state (`in_sync`, `pending`, `conflict`, `error`), last sync and open "
        "conflicts. Requires `manage` on the publication's catalog."
    ),
    input_schema={
        "type": "object",
        "properties": {"entry_id": UUID_SCHEMA},
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"sources": {"type": "array", "items": ENTRY_SYNC_SCHEMA}},
        "required": ["sources"],
    },
    access=ToolAccess.USER,
)
def get_entry_sync(request, raw_arguments: dict) -> dict:
    from apps.harvest.serializers import EntrySyncSerializer
    from apps.harvest.status import entry_sync

    arguments = Arguments(raw_arguments, allowed=("entry_id",))
    entry = _manageable_entry(request, arguments.uuid("entry_id", required=True))
    return EntrySyncSerializer.model_validate({"sources": entry_sync(entry)}).model_dump(mode="json")


@registry.tool(
    name="push_entry",
    title="Push a publication to a synced system",
    description=(
        "Send a publication's metadata to a two-way `sync` source now (a `harvest.push` background job): it is "
        "merged with the remote record field by field (the later edit wins, conflicts are kept), or created "
        "remotely when not linked yet. Requires `manage` on the catalog. Without `source_id` every enabled sync "
        "source of the catalog is pushed to."
    ),
    input_schema={
        "type": "object",
        "properties": {"entry_id": UUID_SCHEMA, "source_id": UUID_SCHEMA},
        "required": ["entry_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "job_id": UUID_SCHEMA,
                        "source_id": UUID_SCHEMA,
                        "state": {"type": "string"},
                        "reason": {"type": ["string", "null"]},
                    },
                },
            }
        },
        "required": ["jobs"],
    },
    access=ToolAccess.WRITE,
    idempotent=False,
)
def push_entry(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("entry_id", "source_id"))
    entry = _manageable_entry(request, arguments.uuid("entry_id", required=True))
    sources = HarvestSource.objects.filter(
        target_catalog_id=entry.catalog_id, mode=HarvestSource.Mode.SYNC, enabled=True
    )
    source_id = arguments.uuid("source_id")
    if source_id:
        sources = sources.filter(pk=source_id)
    sources = list(sources)
    if not sources:
        raise ToolError("No enabled two-way sync source holds this publication's catalog.")
    jobs = []
    for source in sources:
        try:
            job = declare("harvest.push", entry.pk, spec={"source": str(source.pk)})
        except DispatchError as error:
            if error.status == 409:
                raise ToolConflict(error.message)
            raise ToolError(error.message)
        jobs.append({"job_id": str(job.pk), "source_id": str(source.pk), "state": job.state, "reason": job.reason})
    logger.info("mcp.write user=%s action=push_entry entry=%s sources=%d", request.user.pk, entry.pk, len(jobs))
    return {"jobs": jobs}


__all__ = [
    "get_entry_sync",
    "get_harvest_source",
    "list_harvest_records",
    "list_harvest_sources",
    "push_entry",
    "run_harvest",
]
