from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.shelf_records import ShelfRecordFilter
from apps.api.forms.shelf_records import ShelfRecordForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.shelf_records import ShelfRecordSerializer
from apps.core.errors import ValidationException, UnauthorizedException, ProblemDetailException
from apps.core.models import ShelfRecord
from apps.core.views import SecuredView


class ShelfRecordManagement(SecuredView):
    def get(self, request):
        shelf_records = ShelfRecordFilter(
            request.GET, queryset=ShelfRecord.objects.filter(user=request.user), request=request
        ).qs

        return PaginationResponse(request, shelf_records, serializer=ShelfRecordSerializer.Base)

    def post(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(request, detail=_("You need to be logged in to access shelf"))

        form = ShelfRecordForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        shelf_record, created = ShelfRecord.objects.get_or_create(
            user=request.user,
            entry=form.cleaned_data['entry_id']
        )

        if not created:
            return SingleResponse(request, shelf_record, serializer=ShelfRecordSerializer.Base, status=HTTPStatus.OK)

        return SingleResponse(request, shelf_record, serializer=ShelfRecordSerializer.Base, status=HTTPStatus.CREATED)


class ShelfRecordDetail(SecuredView):
    def delete(self, request, shelf_record_id: UUID):
        try:
            shelf_record = ShelfRecord.objects.get(pk=shelf_record_id)
        except ShelfRecord.DoesNotExist as e:
            raise ProblemDetailException(request, _("Not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission('check_shelf_record_access', request.user, shelf_record):
            raise ProblemDetailException(request, _("Not found"), status=HTTPStatus.NOT_FOUND)

        shelf_record.delete()

        return SingleResponse(request)
