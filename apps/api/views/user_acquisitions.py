from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.user_acquisitions import UserAcquisitionFilter
from apps.api.forms.user_acquisitions import UserAcquisitionForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.user_acquisitions import UserAcquisitionSerializer
from apps.core.errors import ValidationException, ProblemDetailException
from apps.core.models import UserAcquisition
from apps.core.views import SecuredView


class UserAcquisitionManagement(SecuredView):
    def get(self, request):
        user_acquisitions = UserAcquisitionFilter(
            request.GET, queryset=UserAcquisition.objects.all(), request=request
        ).qs

        return PaginationResponse(request, user_acquisitions, serializer=UserAcquisitionSerializer.Base)

    def post(self, request):
        form = UserAcquisitionForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if not has_object_permission('check_entry_read', request.user, form.cleaned_data['acquisition_id'].entry):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        user_acquisition = UserAcquisition(user=request.user)
        form.populate(user_acquisition)
        user_acquisition.save()

        return SingleResponse(
            request, user_acquisition, serializer=UserAcquisitionSerializer.Base, status=HTTPStatus.CREATED
        )


class UserAcquisitionDetail(SecuredView):
    def get(self, request, user_acquisition_id: UUID):
        try:
            user_acquisition = UserAcquisition.objects.get(pk=user_acquisition_id)
        except UserAcquisition.DoesNotExist as e:
            raise ProblemDetailException(
                request,
                _("User acquisition not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=ProblemDetailException.DetailType.NOT_FOUND
            )

        if not has_object_permission('check_user_acquisition_read', request.user, user_acquisition):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return SingleResponse(request, user_acquisition, serializer=UserAcquisitionSerializer.Base)
