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
    @openapi.metadata(
        description="Retrieve a paginated list of shelf records for the authenticated user. Returns all books, entries, and catalog items that the user has added to their personal shelf. Requires authentication and filters results to only show the current user's shelf items.",
        tags=["Shelf Records"],
        summary="List user's shelf records",
    )
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

    @openapi.metadata(
        description="Add a new entry to the user's shelf. Creates a shelf record linking the authenticated user to a specific catalog entry. If the entry is already on the user's shelf, returns the existing record with HTTP 200 status. Requires authentication and valid entry ID.",
        tags=["Shelf Records"],
        summary="Add entry to user's shelf",
    )
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
    @openapi.metadata(
        description="Remove a specific entry from the user's shelf. Permanently deletes the shelf record identified by the provided UUID. Only the shelf record owner can delete their own records. Returns 404 if the record doesn't exist or the user lacks permission.",
        tags=["Shelf Records"],
        summary="Remove entry from user's shelf",
    )
    def delete(self, request, shelf_record_id: UUID):
        try:
            shelf_record = ShelfRecord.objects.get(pk=shelf_record_id)
        except ShelfRecord.DoesNotExist as e:
            raise ProblemDetailException(title=_("Not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission("check_shelf_record_access", request.user, shelf_record):
            raise ProblemDetailException(title=_("Not found"), status=HTTPStatus.NOT_FOUND)

        shelf_record.delete()

        return SingleResponse(request)
