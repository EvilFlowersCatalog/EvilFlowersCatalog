"""
License Management Views

Handles license CRUD operations and state management.
Uses the new service layer for all business logic.
"""

from datetime import datetime
from http import HTTPStatus
from uuid import UUID

from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
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
from apps.readium.services import LicenseService, PassphraseRequiredError
from apps.readium.services.renew_policy import evaluate_renew


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

        except PassphraseRequiredError as e:
            raise ProblemDetailException(
                _("LCP passphrase required"),
                detail=str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.PASSPHRASE_REQUIRED,
                additional_data={"set_passphrase_url": "/api/v1/users/me"},
                previous=e,
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

        if not has_object_permission("check_license_manage", request.user, license):
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


class LicenseRenewalsView(SecuredView):
    """
    Renewal sub-resource of License (IP-003 Phase 3e).

    POST /readium/v1/licenses/{license_id}/renewals
        Body: { "requested_end": "<iso-8601>" }
        On allow: forwards to LicenseService.renew_license and returns the LSD
        status document (proxied) on 200.
        On deny: RFC 7807 problem-details JSON with status 403.

    This endpoint is pointed at by the LCP Status Server's `renew_custom_url`.
    """

    @openapi.metadata(
        description=(
            "Renew a license. Body `{\"requested_end\": \"<iso-8601>\"}`. "
            "STU policy: denied if a reservation queue exists on the entry, if the license is within "
            "the post-acquisition embargo, or if the requested end exceeds the max renewal window."
        ),
        tags=["Licenses"],
        summary="Create license renewal",
    )
    def post(self, request, license_id: UUID):
        try:
            license_obj = License.objects.get(pk=license_id)
        except License.DoesNotExist as e:
            raise ProblemDetailException(
                _("License not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        requested_end_value: datetime = None
        if request.body:
            import json

            try:
                payload = json.loads(request.body or b"{}")
            except json.JSONDecodeError:
                raise ProblemDetailException(_("Invalid JSON body"), status=HTTPStatus.BAD_REQUEST)
            raw = payload.get("requested_end") if isinstance(payload, dict) else None
            if raw:
                requested_end_value = parse_datetime(raw)
                if requested_end_value is None:
                    raise ProblemDetailException(
                        _("`requested_end` must be an ISO-8601 datetime"),
                        status=HTTPStatus.BAD_REQUEST,
                        detail_type=DetailType.VALIDATION_ERROR,
                    )

        decision = evaluate_renew(license_obj, requested_end=requested_end_value)
        if not decision.allowed:
            raise ProblemDetailException(
                _("Renewal denied"),
                detail=decision.reason,
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.CONFLICT,
            )

        # Compute the duration in days that LicenseService.renew_license expects.
        from django.utils import timezone

        days = max(1, int((decision.new_end - timezone.now()).total_seconds() // 86400))
        try:
            LicenseService.renew_license(license_obj, new_duration_days=days)
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to renew license"),
                detail=str(e),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                previous=e,
            )

        return JsonResponse(
            {
                "license_id": str(license_obj.pk),
                "expires_at": license_obj.expires_at.isoformat() if license_obj.expires_at else None,
            },
            status=HTTPStatus.OK,
            content_type="application/vnd.readium.license.status.v1.0+json",
        )
