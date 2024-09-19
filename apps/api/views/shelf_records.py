from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.shelf_records import ShelfRecordFilter
from apps.api.forms.shelf_records import ShelfRecordForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.shelf_records import ShelfRecordSerializer
from apps.core.errors import (
    ValidationException,
    UnauthorizedException,
    ProblemDetailException,
)
from apps.core.models import ShelfRecord
from apps.core.views import SecuredView


class ShelfRecordManagement(SecuredView):
    @openapi.metadata(description="List ShelfRecords", tags=["Shelf Records"])
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to access shelf"))

        shelf_records = ShelfRecordFilter(
            request.GET,
            queryset=ShelfRecord.objects.filter(user=request.user),
            request=request,
        ).qs

        return PaginationResponse(
            request, shelf_records, serializer=ShelfRecordSerializer.Base, serializer_context={"request": request}
        )

    @openapi.metadata(description="Create ShelfRecord", tags=["Shelf Records"])
    def post(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to access shelf"))

        form = ShelfRecordForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        shelf_record, created = ShelfRecord.objects.get_or_create(
            user=request.user, entry=form.cleaned_data["entry_id"]
        )

        if not created:
            return SingleResponse(
                request,
                data=ShelfRecordSerializer.Base.model_validate(shelf_record, context={"request": request}),
                status=HTTPStatus.OK,
            )

        return SingleResponse(
            request,
            data=ShelfRecordSerializer.Base.model_validate(shelf_record, context={"request": request}),
            status=HTTPStatus.CREATED,
        )


class ShelfRecordDetail(SecuredView):
    @openapi.metadata(description="ShelfRecord detail", tags=["Shelf Records"])
    def delete(self, request, shelf_record_id: UUID):
        try:
            shelf_record = ShelfRecord.objects.get(pk=shelf_record_id)
        except ShelfRecord.DoesNotExist as e:
            raise ProblemDetailException(title=_("Not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission("check_shelf_record_access", request.user, shelf_record):
            raise ProblemDetailException(title=_("Not found"), status=HTTPStatus.NOT_FOUND)

        shelf_record.delete()

        return SingleResponse(request)
