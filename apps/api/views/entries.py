import json
import mimetypes
from http import HTTPStatus
from uuid import uuid4, UUID

from django.db import transaction
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.services.entry_introspection_service import EntryIntrospectionService
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import EntryForm, AcquisitionMetaForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import EntrySerializer, AcquisitionSerializer
from apps.api.services.entry import EntryService
from apps.core.models import Entry, Acquisition, Price, Catalog, ShelfRecord, User
from apps.core.views import SecuredView


def shelf_record_mapping(user: User) -> dict[UUID, UUID]:
    return {
        shelf_record["entry_id"]: shelf_record["id"]
        for shelf_record in ShelfRecord.objects.filter(user=user).values("entry_id", "id")
    }


class EntryPaginator(SecuredView):
    @openapi.metadata(description="List Entries", tags=["Entries"])
    def get(self, request):
        entries = (
            EntryFilter(request.GET, queryset=Entry.objects.all(), request=request)
            .qs.distinct()
            .prefetch_related("acquisitions")
            .all()
        )

        return PaginationResponse(
            request,
            entries,
            serializer=EntrySerializer.Base,
            serializer_context={"shelf_entries": shelf_record_mapping(request.user), "request": request},
        )


class EntryIntrospection(SecuredView):
    @openapi.metadata(description="Entry Introspection", tags=["Entries"])
    def get(self, request):
        service = EntryIntrospectionService(request.GET.get("driver"))
        result = service.resolve(request.GET.get("identifier"))

        return SingleResponse(request, data=result)


class EntryManagement(SecuredView):
    @openapi.metadata(description="Create Entry object", tags=["Entries"])
    @method_decorator(transaction.atomic)
    def post(self, request, catalog_id: UUID):
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(_("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission("check_catalog_write", request.user, catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        form = EntryForm.create_from_request(request)
        form.fields["category_ids"].queryset = form.fields["category_ids"].queryset.filter(catalog=catalog)
        form.fields["author_ids"].queryset = form.fields["author_ids"].queryset.filter(catalog=catalog)

        if not form.is_valid():
            raise ValidationException(form)

        entry = Entry(creator=request.user, catalog=catalog)
        service = EntryService(catalog, request.user)

        try:
            service.populate(entry, form)
        except EntryService.AlreadyExists as e:
            raise ProblemDetailException(
                "Entry already exists!",
                status=HTTPStatus.CONFLICT,
                detail=_("Entry with same title, isbn or DOI already exists in catalog %s") % (catalog.title,),
                previous=e,
                detail_type=DetailType.CONFLICT,
            )

        return SingleResponse(
            request,
            data=EntrySerializer.Detailed.model_validate(
                entry, context={"shelf_entries": shelf_record_mapping(request.user), "request": request}
            ),
            status=HTTPStatus.CREATED,
        )


class EntryDetail(SecuredView):
    @staticmethod
    def get_entry(
        request,
        catalog_id: UUID,
        entry_id: UUID,
        checker: str = "check_entry_manage",
    ) -> Entry:
        try:
            entry = Entry.objects.get(pk=entry_id, catalog_id=catalog_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Entry not found"), status=HTTPStatus.NOT_FOUND)

        if not has_object_permission(checker, request.user, entry):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return entry

    @openapi.metadata(description="Gent Entry detail", tags=["Entries"])
    def get(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id, "check_entry_read")

        return SingleResponse(
            request,
            data=EntrySerializer.Detailed.model_validate(
                entry, context={"shelf_entries": shelf_record_mapping(request.user), "request": request}
            ),
        )

    @openapi.metadata(description="Create Entry", tags=["Entries"])
    def post(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id)

        try:
            metadata = json.loads(request.POST.get("metadata", "{}"))
        except json.JSONDecodeError as e:
            raise ProblemDetailException(
                title=_("Unable to parse metadata for request file"),
                status=HTTPStatus.BAD_REQUEST,
                previous=e,
            )

        form = AcquisitionMetaForm(metadata, request)

        if not form.is_valid():
            raise ValidationException(form)

        acquisition = Acquisition(
            entry=entry,
            relation=form.cleaned_data.get("relation", Acquisition.AcquisitionType.ACQUISITION),
            mime=request.FILES["content"].content_type,
        )

        if "content" in request.FILES.keys():
            acquisition.content.save(
                f"{uuid4()}{mimetypes.guess_extension(acquisition.mime)}",
                request.FILES["content"],
            )

        for price in form.cleaned_data.get("prices", []):
            Price.objects.create(
                acquisition=acquisition,
                currency=price["currency_code"],
                value=price["value"],
            )

        return SingleResponse(
            request,
            data=AcquisitionSerializer.Detailed.model_validate(acquisition, context={"request": request}),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(description="Update Entry", tags=["Entries"])
    def put(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id)

        form = EntryForm.create_from_request(request)
        form.fields["category_ids"].queryset = form.fields["category_ids"].queryset.filter(catalog_id=catalog_id)
        form.fields["author_ids"].queryset = form.fields["author_ids"].queryset.filter(catalog_id=catalog_id)

        if not form.is_valid():
            raise ValidationException(form)

        catalog = Catalog.objects.get(pk=catalog_id)
        service = EntryService(catalog, request.user)

        try:
            service.populate(entry, form)
        except EntryService.AlreadyExists as e:
            raise ProblemDetailException(
                "Entry already exists!",
                status=HTTPStatus.CONFLICT,
                previous=e,
                detail=_("Entry with same title, isbn or DOI already exists in catalog %s") % (catalog.title,),
            )

        return SingleResponse(
            request,
            data=EntrySerializer.Detailed.model_validate(
                entry, context={"shelf_entries": shelf_record_mapping(request.user), "request": request}
            ),
        )

    @openapi.metadata(description="Delete Entry", tags=["Entries"])
    def delete(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id)
        entry.delete()

        return SingleResponse(request)
