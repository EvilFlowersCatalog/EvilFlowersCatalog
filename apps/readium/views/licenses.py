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
from apps.readium.filters import LicenseFilter
from apps.readium.forms import CreateLicenseForm, UpdateLicenseForm
from apps.readium.models import License
from apps.readium.serializers import LicenseSerializer
from apps.readium.lcp_client import LCPClient


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
        description="Create a new license for the authenticated user. Generates a license with specified duration, automatically setting start and expiration dates. If no start date is provided, defaults to current timestamp. Returns the created license with all metadata.",
        tags=["Licenses"],
        summary="Create new license",
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
        summary="Get license details",
    )
    def get(self, request, license_id: UUID):
        license = self._get_license(request, license_id)
        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))

    @openapi.metadata(
        description="Download the LCP license file for a specific license. Returns the actual LCP license JSON that can be imported into reading applications.",
        tags=["Licenses"],
        summary="Download LCP license file",
    )
    def download(self, request, license_id: UUID):
        license = self._get_license(request, license_id)

        # Check if license has an LCP license ID
        if not license.lcp_license_id:
            raise ProblemDetailException(
                _("License not yet generated"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        # Check if license is still valid
        if license.state == License.LicenseState.REVOKED:
            raise ProblemDetailException(
                _("License has been revoked"),
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.FORBIDDEN,
            )

        if license.is_expired:
            raise ProblemDetailException(
                _("License has expired"),
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.FORBIDDEN,
            )

        # Use LCP client to fetch fresh license
        lcp_client = LCPClient()
        try:
            fresh_license = lcp_client.fetch_fresh_license(license)

            # Return as downloadable LCP license
            response = JsonResponse(fresh_license, content_type="application/vnd.readium.lcp.license.v1.0+json")
            response["Content-Disposition"] = f'attachment; filename="{license.entry.title}.lcpl"'
            return response

        except Exception as e:
            raise ProblemDetailException(
                _("Failed to generate license file"),
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
        """Handle license state transitions with automatic LCP operations"""
        lcp_client = LCPClient()

        if new_state == "active" and license.state == License.LicenseState.READY:
            # Device registration
            license.state = License.LicenseState.ACTIVE
            license.device_count += 1

        elif new_state == "returned" and license.state == License.LicenseState.ACTIVE:
            # Return license
            try:
                lcp_client.return_license(license)
                license.state = License.LicenseState.RETURNED
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to return license"), detail=str(e), status=HTTPStatus.INTERNAL_SERVER_ERROR
                )

        elif new_state == "renewed" and license.state == License.LicenseState.ACTIVE:
            # Renew license
            new_end_date = data.get("expires_at")
            if new_end_date:
                try:
                    from django.utils.dateparse import parse_datetime

                    expires_at = parse_datetime(new_end_date)
                    if expires_at:
                        lcp_client.renew_license(license, expires_at)
                        license.expires_at = expires_at
                except Exception as e:
                    raise ProblemDetailException(
                        _("Failed to renew license"), detail=str(e), status=HTTPStatus.INTERNAL_SERVER_ERROR
                    )
            else:
                # Default 14-day renewal
                from datetime import timedelta

                new_end_date = timezone.now() + timedelta(days=14)
                try:
                    lcp_client.renew_license(license, new_end_date)
                    license.expires_at = new_end_date
                except Exception as e:
                    raise ProblemDetailException(
                        _("Failed to renew license"), detail=str(e), status=HTTPStatus.INTERNAL_SERVER_ERROR
                    )

        elif new_state == "revoked":
            # Revoke license
            reason = data.get("reason", "Revoked by administrator")
            try:
                lcp_client.revoke_license(license, reason)
                license.state = License.LicenseState.REVOKED
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to revoke license"), detail=str(e), status=HTTPStatus.INTERNAL_SERVER_ERROR
                )
