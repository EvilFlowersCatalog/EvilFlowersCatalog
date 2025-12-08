"""
License Management Views

Handles license CRUD operations and state management.
Uses the new service layer for all business logic.
"""

from http import HTTPStatus
from uuid import UUID
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import PaginationResponse, SingleResponse
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.views import SecuredView
from apps.core.models import Entry
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
        licenses = LicenseFilter(request.GET, queryset=License.objects.all(), request=request).qs

        return PaginationResponse(request, licenses, serializer=LicenseSerializer.Base)

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
        - user_passphrase: User's chosen passphrase for decryption
        - passphrase_hint: Hint for the passphrase (optional)
        - duration_days: License duration in days (default: 14)
        """,
        tags=["Licenses"],
        summary="Create new LCP license",
    )
    def post(self, request):
        # Extract required parameters
        entry_id = request.data.get("entry_id")
        user_passphrase = request.data.get("user_passphrase")
        passphrase_hint = request.data.get("passphrase_hint")
        duration_days = int(request.data.get("duration_days", 14))
        start_date_str = request.data.get("start_date")

        # Validate required fields
        if not entry_id:
            raise ProblemDetailException(
                _("entry_id is required"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        if not user_passphrase:
            raise ProblemDetailException(
                _("user_passphrase is required"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        # Get entry
        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        # Parse start date if provided
        start_date = None
        if start_date_str:
            from django.utils.dateparse import parse_datetime
            start_date = parse_datetime(start_date_str)

        # Create license via service
        try:
            license = LicenseService.create_license(
                entry=entry,
                user=request.user,
                user_passphrase=user_passphrase,
                passphrase_hint=passphrase_hint,
                start_date=start_date,
                duration_days=duration_days,
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
        description="""
        Download the LCP license file (.lcpl) for a specific license.

        This endpoint implements the License Gateway pattern per LCP integration guide.
        It retrieves fresh license data from the LCP Server and returns it in the
        standard LCP license format that reading applications can import.

        Reading applications will call this endpoint to:
        - Get the initial license after acquisition
        - Fetch updated licenses after renewal/return
        - Retrieve fresh licenses after modification
        """,
        tags=["Licenses"],
        summary="Download LCP license file (License Gateway)",
    )
    def download(self, request, license_id: UUID):
        """License Gateway implementation."""
        license = self._get_license(request, license_id)

        # Fetch fresh license via service (validates state internally)
        try:
            fresh_license = LicenseService.fetch_fresh_license(license)

            # Return as downloadable LCP license
            response = JsonResponse(
                fresh_license,
                content_type="application/vnd.readium.lcp.license.v1.0+json"
            )
            response["Content-Disposition"] = f'attachment; filename="{license.entry.title}.lcpl"'
            return response

        except ValueError as e:
            # Validation errors (revoked, expired, no lcp_license_id, etc.)
            raise ProblemDetailException(
                str(e),
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.FORBIDDEN,
                previous=e,
            )
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to fetch license file"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail_type=DetailType.INTERNAL_ERROR,
                previous=e,
            )

    @openapi.metadata(
        description="Update license state and properties. Supports state transitions like 'active', 'returned', 'renewed' etc. LCP operations are handled automatically based on state changes.",
        tags=["Licenses"],
        summary="Update license state",
    )
    def put(self, request, license_id: UUID):
        license = self._get_license(request, license_id)

        # Handle state changes
        new_state = request.data.get("state")
        if new_state:
            self._handle_state_change(license, new_state, request.data)

        # Handle other property updates
        form = UpdateLicenseForm.create_from_request(request)
        if form.is_valid():
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
            duration_days = data.get("duration_days", 14)
            try:
                LicenseService.renew_license(license, new_duration_days=duration_days)
                # Note: service updates expir es_at
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to renew license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )

        elif new_state == "revoked":
            # Revoke license via service
            reason = data.get("reason", "Revoked by administrator")
            try:
                LicenseService.revoke_license(license, reason)
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
            reason = data.get("reason", "Cancelled by user")
            try:
                LicenseService.cancel_license(license, reason)
                # Note: service updates state to CANCELLED
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to cancel license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )
