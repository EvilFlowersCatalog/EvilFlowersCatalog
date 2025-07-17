from http import HTTPStatus
from uuid import UUID

from django.utils import timezone
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import PaginationResponse, SingleResponse
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.views import SecuredView
from apps.readium.filters import LicenseFilter
from apps.readium.forms import CreateLicenseForm, UpdateLicenseForm
from apps.readium.models import License
from apps.readium.serializers import LicenseSerializer


class LicenseManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of licenses in the system. Returns license information including duration, start/end dates, user associations, and status. Supports filtering by various license attributes to help manage user access and permissions.",
        tags=["Licenses"],
        summary="List all licenses"
    )
    def get(self, request):
        licenses = LicenseFilter(request.GET, queryset=License.objects.all(), request=request).qs

        return PaginationResponse(request, licenses, serializer=LicenseSerializer.Base)

    @openapi.metadata(
        description="Create a new license for the authenticated user. Generates a license with specified duration, automatically setting start and expiration dates. If no start date is provided, defaults to current timestamp. Returns the created license with all metadata.",
        tags=["Licenses"],
        summary="Create new license"
    )
    def post(self, request):
        form = CreateLicenseForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        license = License(user=request.user)
        form.populate(license)

        if not license.starts_at:
            license.starts_at = timezone.now()

        license.expires_at = license.starts_at + form.cleaned_data["duration"]
        license.save()

        return SingleResponse(
            request,
            data=LicenseSerializer.Base.model_validate(license),
            status=HTTPStatus.CREATED,
        )


class LicenseDetail(SecuredView):
    @staticmethod
    def _get_license(request, license_id: UUID) -> License:
        try:
            license = License.objects.get(pk=license_id)
        except License.DoesNotExist as e:
            raise ProblemDetailException(
                _("License not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not has_object_permission("check_license_manage", request.user, license.user):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return license

    @openapi.metadata(
        description="Retrieve detailed information about a specific license. Returns comprehensive license data including duration, start/end dates, user information, and current status. Requires license manage permissions for the license owner.",
        tags=["Licenses"],
        summary="Get license details"
    )
    def get(self, request, license_id: UUID):
        license = self._get_license(request, license_id)
        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))

    @openapi.metadata(
        description="Update an existing license's information. Allows modification of license properties such as duration and metadata. Requires license manage permissions and validates the requesting user has appropriate access to the license.",
        tags=["Licenses"],
        summary="Update license information"
    )
    def put(self, request, annotation_id: UUID):
        form = UpdateLicenseForm.create_from_request(request)
        license = self._get_license(request, annotation_id)

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(license)
        license.save()

        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))
