from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.user_acquisitions import UserAcquisitionFilter
from apps.api.forms.user_acquisitions import UserAcquisitionForm
from apps.api.response import PaginationResponse, SingleResponse, SeeOtherResponse
from apps.api.serializers.user_acquisitions import UserAcquisitionSerializer
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.models import UserAcquisition
from apps.core.views import SecuredView
from apps.openapi.types import ParameterLocation


class UserAcquisitionManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of user acquisitions across the system. Returns acquisition records that represent user access to specific catalog entries. Supports filtering and pagination to manage large result sets. Includes acquisition details, user information, and associated entry metadata.",
        tags=["User Acquisitions"],
        summary="List all user acquisitions",
    )
    def get(self, request):
        user_acquisitions = UserAcquisitionFilter(
            request.GET, queryset=UserAcquisition.objects.all(), request=request
        ).qs

        return PaginationResponse(
            request,
            user_acquisitions,
            serializer=UserAcquisitionSerializer.Base,
            serializer_context={"request": request},
        )

    @openapi.metadata(
        description="Create a new user acquisition record, granting a user access to a specific catalog entry. Supports both personal and shared acquisition types. Validates user permissions for the target entry and handles single acquisition mode restrictions. Returns existing acquisition with redirect if already exists in single mode.",
        tags=["User Acquisitions"],
        summary="Create new user acquisition",
    )
    def post(self, request):
        form = UserAcquisitionForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission("check_entry_read", request.user, form.cleaned_data["acquisition_id"].entry):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if (
            form.cleaned_data["type"] == UserAcquisition.UserAcquisitionType.PERSONAL
            and settings.EVILFLOWERS_USER_ACQUISITION_MODE == "single"
        ):
            user_acquisition = UserAcquisition.objects.filter(
                acquisition=form.cleaned_data["acquisition_id"],
                type=UserAcquisition.UserAcquisitionType.PERSONAL,
                user=request.user,
            ).first()

            if user_acquisition:
                return SeeOtherResponse(
                    redirect_to=request.build_absolute_uri(
                        f"{reverse('api:user-acquisition-detail', kwargs={'user_acquisition_id': user_acquisition.pk})}"
                    )
                )

        user_acquisition = UserAcquisition(user=request.user)
        form.populate(user_acquisition)

        evilflowers_share_enabled = user_acquisition.acquisition.entry.read_config("evilflowers_share_enabled")
        if user_acquisition.type == UserAcquisition.UserAcquisitionType.SHARED and not evilflowers_share_enabled:
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        user_acquisition.save()

        return SingleResponse(
            request,
            data=UserAcquisitionSerializer.Base.model_validate(
                user_acquisition, context={"user": request.user, "request": request}
            ),
            status=HTTPStatus.CREATED,
        )


@openapi.parameter(
    name="user_acquisition_id", param_type=UUID, location=ParameterLocation.PATH, description="UserAcquisition UUID"
)
class UserAcquisitionDetail(SecuredView):
    @openapi.metadata(
        description="Retrieve detailed information about a specific user acquisition record. Returns acquisition metadata, associated entry details, user information, and access permissions. Validates that the requesting user has permission to view the acquisition record.",
        tags=["User Acquisitions"],
        summary="Get user acquisition details",
    )
    def get(self, request, user_acquisition_id: UUID):
        try:
            user_acquisition = UserAcquisition.objects.get(pk=user_acquisition_id)
        except UserAcquisition.DoesNotExist as e:
            raise ProblemDetailException(
                _("User acquisition not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not has_object_permission("check_user_acquisition_read", request.user, user_acquisition):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return SingleResponse(
            request,
            data=UserAcquisitionSerializer.Base.model_validate(
                user_acquisition, context={"user": request.user, "request": request}
            ),
        )
