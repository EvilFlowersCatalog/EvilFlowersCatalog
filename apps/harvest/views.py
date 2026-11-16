"""IP-034 admin API for the portal: harvest sources, their records, a connection test, MARC uploads, and the
configured external systems. Runs are IP-031 jobs (`POST /api/v1/jobs` kind `harvest.run`), not endpoints here.

Every `harvest/*` endpoint needs `manage` on the source's target catalog, or superuser. An unreadable source
answers 404, like the other catalog-scoped endpoints.
"""

import uuid
from http import HTTPStatus
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException, ValidationException
from apps.core.identifiers import external_systems
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.files.storage import get_storage
from apps.harvest import exposure
from apps.harvest.adapters import HarvestError, connector_for, registry as adapters
from apps.harvest.adapters.marc_file import is_xml
from apps.harvest.adapters.registry import describe
from apps.harvest.filters import HarvestRecordFilter, HarvestSourceFilter, manageable_sources
from apps.harvest.forms import HarvestSourceForm, sync_problem
from apps.harvest.models import HarvestConflict, HarvestRecord, HarvestSource
from apps.harvest.status import entry_sync
from apps.harvest.serializers import (
    AdapterTypeSerializer,
    ConflictSerializer,
    EntrySyncSerializer,
    ExternalSystemSerializer,
    HarvestRecordSerializer,
    HarvestSourceSerializer,
    SourceProbeSerializer,
)


def _require_user(request):
    if not request.user.is_authenticated:
        raise UnauthorizedException(detail=_("Harvest sources are managed by catalog managers."))


def _require_manage(request, catalog):
    if not has_object_permission("check_catalog_manage", request.user, catalog):
        raise ProblemDetailException(
            _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN, detail_type=DetailType.FORBIDDEN
        )


def _get_source(request, source_id: UUID) -> HarvestSource:
    _require_user(request)
    source = manageable_sources(request.user).filter(pk=source_id).first()
    if source is None:
        raise ProblemDetailException(
            _("Harvest source not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
        )
    return source


def _save(source: HarvestSource) -> None:
    problem = sync_problem(source)
    if problem:
        raise ProblemDetailException(
            problem, status=HTTPStatus.UNPROCESSABLE_ENTITY, detail_type=DetailType.VALIDATION_ERROR
        )
    try:
        with transaction.atomic():
            source.save()
        exposure.forget()
    except IntegrityError as error:
        raise ProblemDetailException(
            _("Harvest source with slug %s already exists") % source.slug,
            status=HTTPStatus.CONFLICT,
            detail_type=DetailType.CONFLICT,
            previous=error,
        )


class HarvestSourceManagement(SecuredView):
    @openapi.metadata(
        description=(
            "Harvest sources whose target catalog the caller manages (all for a superuser) (IP-034). Filters: "
            "`target_catalog_id`, `adapter`, `enabled`, `query` (slug or name)."
        ),
        tags=["Harvest"],
        summary="List harvest sources",
    )
    def get(self, request):
        _require_user(request)
        sources = HarvestSourceFilter(request.GET, queryset=manageable_sources(request.user), request=request).qs
        return PaginationResponse(request, sources, serializer=HarvestSourceSerializer.Base)

    @openapi.metadata(
        description=(
            "Create a harvest source (IP-034). Requires `manage` on `target_catalog_id`. `system_key` must be a "
            "configured external system (`GET /api/v1/external-systems`). Sources start disabled: dry runs work, "
            "real runs need `enabled: true`."
        ),
        tags=["Harvest"],
        summary="Create harvest source",
    )
    @openapi.response(HTTPStatus.CREATED, "Harvest source created", HarvestSourceSerializer.Detailed)
    def post(self, request):
        _require_user(request)
        form = HarvestSourceForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)
        _require_manage(request, form.cleaned_data["target_catalog_id"])

        source = form.apply(HarvestSource(creator=request.user))
        _save(source)
        return SingleResponse(request, data=HarvestSourceSerializer.Detailed.of(source), status=HTTPStatus.CREATED)


class HarvestSourceDetail(SecuredView):
    @openapi.metadata(
        description="One harvest source with its record counts per status (real runs and the last dry run).",
        tags=["Harvest"],
        summary="Get harvest source",
    )
    def get(self, request, source_id: UUID):
        source = _get_source(request, source_id)
        return SingleResponse(request, data=HarvestSourceSerializer.Detailed.of(source))

    @openapi.metadata(
        description=(
            "Update a harvest source. The required fields are sent every time; optional fields not sent keep "
            "their value. The slug cannot change: created entries carry it in `origin`. `cursor: null` makes the "
            "next run start from the beginning. Moving the source to another catalog needs `manage` on both."
        ),
        tags=["Harvest"],
        summary="Update harvest source",
    )
    def put(self, request, source_id: UUID):
        source = _get_source(request, source_id)
        form = HarvestSourceForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)
        if form.cleaned_data["slug"] != source.slug:
            raise ProblemDetailException(
                _("The slug of a harvest source cannot change."),
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail_type=DetailType.VALIDATION_ERROR,
            )
        _require_manage(request, form.cleaned_data["target_catalog_id"])
        form.apply(source)
        _save(source)
        return SingleResponse(request, data=HarvestSourceSerializer.Detailed.of(source))

    @openapi.metadata(
        description=(
            "Delete a harvest source and its mirror records. Entries it created or linked stay, with their "
            "`identifiers` key and `origin`."
        ),
        tags=["Harvest"],
        summary="Delete harvest source",
    )
    def delete(self, request, source_id: UUID):
        source = _get_source(request, source_id)
        upload_path = source.upload_path
        source.delete()
        exposure.forget()
        if upload_path:
            transaction.on_commit(lambda: _delete_file(upload_path))
        return SingleResponse(request)


class HarvestSourceTest(SecuredView):
    @openapi.metadata(
        description=(
            "Try a source without writing anything: OAI-PMH `Identify`, `ListMetadataFormats`, the first page of "
            "`ListSets` and one sample record mapped to the harvest's record shape; for a `marc_file` source the "
            "first record of the uploaded file. Problems are listed in `errors`."
        ),
        tags=["Harvest"],
        summary="Test harvest source",
    )
    @openapi.response(HTTPStatus.OK, "What the source answered", SourceProbeSerializer)
    def post(self, request, source_id: UUID):
        source = _get_source(request, source_id)
        try:
            probe = connector_for(source).test(source)
        except HarvestError as error:
            probe = {"errors": [str(error)]}
        return SingleResponse(request, data=SourceProbeSerializer.model_validate(probe))


class HarvestSourceUpload(SecuredView):
    @openapi.metadata(
        description=(
            "Upload the MARC file (ISO 2709 or MARCXML) a `marc_file` source runs on: multipart field `file`. It "
            "replaces the previous upload. Start the run with `POST /api/v1/jobs` (kind `harvest.run`)."
        ),
        tags=["Harvest"],
        summary="Upload MARC file",
    )
    def post(self, request, source_id: UUID):
        source = _get_source(request, source_id)
        adapter_type = adapters.get(source.adapter)
        if adapter_type is None or not adapter_type.needs_upload:
            raise ProblemDetailException(
                _("Only sources of an upload adapter (marc_file) take uploads."),
                status=HTTPStatus.CONFLICT,
                detail_type=DetailType.CONFLICT,
            )
        upload = request.FILES.get("file")
        if upload is None or not upload.size:
            raise ProblemDetailException(
                _("Missing or empty `file`."), status=HTTPStatus.BAD_REQUEST, detail_type=DetailType.VALIDATION_ERROR
            )
        head = upload.read(64)
        upload.seek(0)
        xml = is_xml(head)
        if not xml and not head[:5].isdigit():
            raise ProblemDetailException(
                _("The file is neither MARCXML nor ISO 2709 MARC."),
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                detail_type=DetailType.VALIDATION_ERROR,
            )
        # Outside `catalogs/`, so the orphan cleanup (`storage_audit`) never takes it for a lost entry folder.
        path = get_storage().save(f"harvest/{source.pk}/{uuid.uuid4()}{'.xml' if xml else '.mrc'}", upload)
        previous, source.upload_path = source.upload_path, path
        source.save(update_fields=["upload_path", "updated_at"])
        if previous:
            transaction.on_commit(lambda: _delete_file(previous))
        return SingleResponse(request, data=HarvestSourceSerializer.Detailed.of(source))


class HarvestRecordManagement(SecuredView):
    @openapi.metadata(
        description=(
            "The mirror records of a source: what each source record said and what the matcher decided. "
            "Filters: `status` (repeatable), `dry_run`, `query` (record id or title); `page`, `limit`."
        ),
        tags=["Harvest"],
        summary="List harvest records",
    )
    def get(self, request, source_id: UUID):
        source = _get_source(request, source_id)
        records = HarvestRecordFilter(
            request.GET, queryset=HarvestRecord.objects.filter(source=source), request=request
        ).qs
        return PaginationResponse(request, records, serializer=HarvestRecordSerializer.Base)


class ExternalSystemManagement(SecuredView):
    @openapi.metadata(
        description=(
            "External systems an entry can be linked to (`EVILFLOWERS_EXTERNAL_SYSTEMS`): the `identifiers` key, "
            "a name, and the URL template of a record (`{record_id}`)."
        ),
        tags=["Harvest"],
        summary="List external systems",
    )
    def get(self, request):
        _require_user(request)
        items = [ExternalSystemSerializer.model_validate(system.as_dict()) for system in external_systems()]
        return SingleResponse(request, data=items)


class AdapterTypeManagement(SecuredView):
    @openapi.metadata(
        description=(
            "Registered harvest connectors: the `adapter` names a source may use, and whether one needs a base "
            "URL or an uploaded file."
        ),
        tags=["Harvest"],
        summary="List harvest adapters",
    )
    def get(self, request):
        _require_user(request)
        items = [AdapterTypeSerializer.model_validate(describe(item)) for item in adapters.all()]
        return SingleResponse(request, data=items)


def _manageable_entry(request, entry_id: UUID) -> Entry:
    _require_user(request)
    entry = Entry.objects.select_related("catalog").filter(pk=entry_id).first()
    if entry is None or not has_object_permission("check_catalog_read", request.user, entry.catalog):
        raise ProblemDetailException(
            _("Entry not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
        )
    _require_manage(request, entry.catalog)
    return entry


class EntrySyncState(SecuredView):
    @openapi.metadata(
        description=(
            "The sources an entry is linked to (any mode, so link-only catalogue records too), each with the remote "
            "record id and URL, and for `sync` sources the state (`in_sync` · `pending` · `conflict` · `error`), "
            "last sync, versions, last message and open conflicts. Requires `manage` on the entry's catalog."
        ),
        tags=["Harvest"],
        summary="Entry sync state",
    )
    @openapi.response(HTTPStatus.OK, "Linked sources", EntrySyncSerializer)
    def get(self, request, entry_id: UUID):
        entry = _manageable_entry(request, entry_id)
        return SingleResponse(request, data=EntrySyncSerializer.model_validate({"sources": entry_sync(entry)}))


class ConflictManagement(SecuredView):
    @openapi.metadata(
        description=(
            "Fields both sides of a two-way sync changed since the last sync: the later side won, the other value "
            "is kept here. Filters: `source_id`, `entry_id`, `acknowledged`; `page`, `limit`. Only conflicts of "
            "sources whose catalog the caller manages."
        ),
        tags=["Harvest"],
        summary="List sync conflicts",
    )
    def get(self, request):
        _require_user(request)
        conflicts = HarvestConflict.objects.filter(source__in=manageable_sources(request.user))
        for name in ("source_id", "entry_id"):
            value = request.GET.get(name)
            if value:
                try:
                    conflicts = conflicts.filter(**{name: UUID(value)})
                except ValueError:
                    raise ProblemDetailException(
                        _("Invalid %s") % name, status=HTTPStatus.BAD_REQUEST, detail_type=DetailType.VALIDATION_ERROR
                    )
        acknowledged = request.GET.get("acknowledged")
        if acknowledged in ("true", "false"):
            conflicts = conflicts.filter(acknowledged=acknowledged == "true")
        return PaginationResponse(
            request, conflicts, serializer=ConflictSerializer, ordering=Ordering(["-created_at"])
        )


class ConflictAcknowledge(SecuredView):
    @openapi.metadata(
        description="Mark a sync conflict as seen. Requires `manage` on the source's catalog.",
        tags=["Harvest"],
        summary="Acknowledge sync conflict",
    )
    def post(self, request, conflict_id: UUID):
        _require_user(request)
        conflict = HarvestConflict.objects.filter(pk=conflict_id, source__in=manageable_sources(request.user)).first()
        if conflict is None:
            raise ProblemDetailException(
                _("Conflict not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
            )
        if not conflict.acknowledged:
            conflict.acknowledged = True
            conflict.acknowledged_by = request.user
            conflict.acknowledged_at = timezone.now()
            conflict.save(update_fields=["acknowledged", "acknowledged_by", "acknowledged_at", "updated_at"])
        return SingleResponse(request, data=ConflictSerializer.model_validate(conflict))


def _delete_file(path: str) -> None:
    try:
        get_storage().delete(path)
    except OSError:
        pass


__all__ = [
    "AdapterTypeManagement",
    "ConflictAcknowledge",
    "ConflictManagement",
    "EntrySyncState",
    "ExternalSystemManagement",
    "HarvestRecordManagement",
    "HarvestSourceDetail",
    "HarvestSourceManagement",
    "HarvestSourceTest",
    "HarvestSourceUpload",
]
