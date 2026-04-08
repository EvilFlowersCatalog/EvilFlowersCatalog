"""
License Management Views

Handles license CRUD operations and state management.
Uses the new service layer for all business logic.
"""

from http import HTTPStatus
from uuid import UUID

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
from apps.readium.services import LicenseService


class LicenseManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of licenses in the system. Returns license information including duration, start/end dates, user associations, and status. Supports filtering by various license attributes to help manage user access and permissions.",
        tags=["Licenses"],
        summary="List all licenses",
    )
    def get(self, request):
        # TODO: prefetch entries
        licenses = LicenseFilter(request.GET, queryset=License.objects.all(), request=request).qs

        return PaginationResponse(
            request, licenses, serializer=LicenseSerializer.Detailed, serializer_context={"request": request}
        )

    @openapi.metadata(
        description="""
        Create a new LCP license for a readium-enabled entry.

        This endpoint handles the complete license creation workflow:
        1. Validates availability (checks concurrent license limits)
        2. Ensures content is encrypted and registered with LCP Server
        3. Generates LCP license with user passphrase
        4. Registers license with Status Server

        Required fields:
        - entry_id: UUID of the entry to license
        - duration: License duration (e.g. "14 00:00:00" for 14 days)

        Optional fields:
        - starts_at: License start date (default: now)
        """,
        tags=["Licenses"],
        summary="Create new LCP license",
    )
    def post(self, request):
        form = CreateLicenseForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        try:
            license = LicenseService.create_license(
                entry=form.cleaned_data["entry_id"],
                user=request.user,
                start_date=form.cleaned_data.get("starts_at"),
                duration_days=form.cleaned_data["duration"].days,
            )

            return SingleResponse(
                request,
                data=LicenseSerializer.Base.model_validate(license),
                status=HTTPStatus.CREATED,
            )

        except ValueError as e:
            raise ProblemDetailException(
                str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
                previous=e,
            )
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to create license"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail_type=DetailType.INTERNAL_ERROR,
                previous=e,
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
        summary="Get license details",
    )
    def get(self, request, license_id: UUID):
        license = self._get_license(request, license_id)
        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))

    @openapi.metadata(
        description="Update license state and properties. Supports state transitions like 'active', 'returned', 'renewed' etc. LCP operations are handled automatically based on state changes.",
        tags=["Licenses"],
        summary="Update license state",
    )
    def put(self, request, license_id: UUID):
        license = self._get_license(request, license_id)

        form = UpdateLicenseForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        new_state = form.cleaned_data.get("state")
        if new_state:
            self._handle_state_change(license, new_state, form.cleaned_data)

        form.populate(license)
        license.save()
        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))

    def _handle_state_change(self, license: License, new_state: str, data: dict):
        """Handle license state transitions with automatic LCP operations via service layer."""

        if new_state == "active" and license.state == License.LicenseState.READY:
            # Device registration (device tracking happens in Status Server)
            license.state = License.LicenseState.ACTIVE
            license.device_count += 1

        elif new_state == "returned":
            # Return license via service
            try:
                LicenseService.return_license(license)
                # Note: service updates state to RETURNED
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to return license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )

        elif new_state == "renewed":
            # Renew license via service
            duration = data.get("duration")
            duration_days = duration.days if duration else 14
            try:
                LicenseService.renew_license(license, new_duration_days=duration_days)
                # Note: service updates expires_at
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to renew license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )

        elif new_state == "revoked":
            # Revoke license via service
            try:
                LicenseService.revoke_license(license)
                # Note: service updates state to REVOKED
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to revoke license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )

        elif new_state == "cancelled":
            # Cancel license via service
            try:
                LicenseService.cancel_license(license)
                # Note: service updates state to CANCELLED
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to cancel license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )
