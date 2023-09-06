from http import HTTPStatus

from django.shortcuts import resolve_url
from django.utils.translation import gettext as _

from apps.api.forms.shelf_records import ShelfRecordForm
from apps.api.response import SingleResponse, SeeOtherResponse
from apps.api.serializers.shelf_records import ShelfRecordSerializer
from apps.core.errors import ValidationException, UnauthorizedException
from apps.core.models import ShelfRecord
from apps.core.views import SecuredView


class ShelfRecordManagement(SecuredView):
    def post(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(request, detail=_("You need to be logged in to access shelf"))

        form = ShelfRecordForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        created, shelf_record = ShelfRecord.objects.get_or_create(
            user=request.user,
            entry=form.cleaned_data['entry_id']
        )

        if not created:
            return SeeOtherResponse(resolve_url('shelf-record-detail', shelf_record_id=shelf_record.id))

        return SingleResponse(request, shelf_record, serializer=ShelfRecordSerializer.Base, status=HTTPStatus.CREATED)


class ShelfRecordDetail(SecuredView):
    pass
