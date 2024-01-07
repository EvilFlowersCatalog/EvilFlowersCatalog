from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.user_acquisitions import UserAcquisitionFilter
from apps.api.forms.user_acquisitions import UserAcquisitionForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.user_acquisitions import UserAcquisitionSerializer
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
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

        if not has_object_permission("check_entry_read", request.user, form.cleaned_data["acquisition_id"].entry):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if (
            form.cleaned_data["type"] == UserAcquisition.UserAcquisitionType.PERSONAL
            and settings.EVILFLOWERS_USER_ACQUISITION_MODE == "single"
        ):
            user_acquisition = UserAcquisition.objects.filter(
                acquisition_id=form.cleaned_data["acquisition_id"],
                type=UserAcquisition.UserAcquisitionType.PERSONAL,
                user=request.user,
            ).first()

            if user_acquisition:
                location = (
                    f"{settings.BASE_URL}"
                    f"{reverse('user-acquisition-detail', kwargs={'user_acquisition_id': user_acquisition.pk})}"
                )
                return HttpResponseRedirect(location, status=HTTPStatus.SEE_OTHER)

        user_acquisition = UserAcquisition(user=request.user)
        form.populate(user_acquisition)

        evilflowers_share_enabled = user_acquisition.acquisition.entry.read_config("evilflowers_share_enabled")
        if user_acquisition.type == UserAcquisition.UserAcquisitionType.SHARED and not evilflowers_share_enabled:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        user_acquisition.save()

        return SingleResponse(
            request,
            UserAcquisitionSerializer.Base.model_validate(user_acquisition, context={"user": request.user}),
            status=HTTPStatus.CREATED
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
                detail_type=DetailType.NOT_FOUND,
            )

        if not has_object_permission("check_user_acquisition_read", request.user, user_acquisition):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return SingleResponse(
            request,
            UserAcquisitionSerializer.Base.model_validate(user_acquisition, context={"user": request.user}),
        )
