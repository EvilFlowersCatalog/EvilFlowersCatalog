"""
License Management Views

Handles license CRUD operations and state management.
Uses the new service layer for all business logic.
"""

import logging
from datetime import datetime
from http import HTTPStatus
from uuid import UUID

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import PaginationResponse, SingleResponse
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.views import SecuredView
from apps.readium.enums import LicenseAction
from apps.readium.filters import LicenseFilter
from apps.readium.forms import CreateLicenseForm, UpdateLicenseForm
from apps.readium.models import License
from apps.readium.serializers import LicenseSerializer
from apps.readium.services import LicenseService, PassphraseRequiredError
from apps.readium.services.renew_policy import evaluate_renew
from apps.readium.views._license_lookup import LicenseLookupMixin

logger = logging.getLogger(__name__)


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


class LicenseDetail(LicenseLookupMixin, SecuredView):
    # IP-004 Phase 4: state-change operations admit catalog managers and
    # superusers in addition to the license owner.
    license_permission = "check_license_state_manage"

    @openapi.metadata(
        description="Retrieve detailed information about a specific license. Returns comprehensive license data including duration, start/end dates, user information, and current status. Requires license manage permissions for the license owner.",
        tags=["Licenses"],
        summary="Get license details",
    )
    def get(self, request, license_id: UUID):
        license = self.get_license_or_404(request, license_id)
        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))

    @openapi.metadata(
        description="Update license state and properties. Supports state transitions like 'active', 'returned', 'renewed' etc. LCP operations are handled automatically based on state changes.",
        tags=["Licenses"],
        summary="Update license state",
    )
    def put(self, request, license_id: UUID):
        form = UpdateLicenseForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        # IP-009 Phase 1 (Q1): emit a single deprecation log per request
        # for legacy `state`/`duration` payloads. The view layer logs;
        # the form layer surfaces the booleans without logging itself.
        if form.cleaned_data.get("_used_legacy_state"):
            logger.warning(
                "license_update_legacy_state_field",
                extra={"license_id": str(license_id)},
            )
        if form.cleaned_data.get("_used_legacy_duration"):
            logger.warning(
                "license_update_legacy_duration_field",
                extra={"license_id": str(license_id)},
            )

        # IP-008 Phase 1 A6: hold the License row lock across state
        # change + save. Without this, the status-proxy
        # DeviceRegistrationProxyView (which also writes device_count
        # and state) can race with this PUT and lose updates.
        with transaction.atomic():
            try:
                license = License.objects.select_for_update().get(pk=license_id)
            except License.DoesNotExist as e:
                raise ProblemDetailException(
                    _("License not found"),
                    status=HTTPStatus.NOT_FOUND,
                    previous=e,
                    detail_type=DetailType.NOT_FOUND,
                )

            if not has_object_permission("check_license_state_manage", request.user, license):
                raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

            action = form.cleaned_data.get("action")
            if action:
                self._dispatch_action(license, action, form.cleaned_data)
                license.save()

        return SingleResponse(request, data=LicenseSerializer.Base.model_validate(license))

    def _dispatch_action(self, license: License, action: str, data: dict):
        """Dispatch a `LicenseAction` to the service layer (IP-009 Phase 1)."""

        if action == LicenseAction.ACTIVATE and license.state == License.LicenseState.READY:
            # Device registration is canonically driven by the Status
            # Server proxy; the PUT path is a UI shortcut.
            license.state = License.LicenseState.ACTIVE
            license.device_count += 1
            return

        if action == LicenseAction.RETURN:
            try:
                LicenseService.return_license(license)
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to return license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )
            return

        if action == LicenseAction.RENEW:
            # Canonical payload: requested_end (ISO-8601 datetime).
            # Legacy: duration → translate to requested_end at the
            # form/view boundary (Q1 resolution).
            requested_end = data.get("requested_end")
            if requested_end is None:
                duration = data.get("duration")
                if duration is not None:
                    requested_end = timezone.now() + duration

            decision = evaluate_renew(license, requested_end=requested_end)
            if not decision.allowed:
                raise ProblemDetailException(
                    _("Renewal denied"),
                    detail=decision.reason,
                    status=HTTPStatus.FORBIDDEN,
                    detail_type=DetailType.CONFLICT,
                )

            try:
                LicenseService.renew_license(license, new_end_date=decision.new_end)
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to renew license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )
            return

        if action == LicenseAction.REVOKE:
            try:
                LicenseService.revoke_license(license)
            except Exception as e:
                raise ProblemDetailException(
                    _("Failed to revoke license"),
                    detail=str(e),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    previous=e,
                )
            return

        if action == LicenseAction.CANCEL:
            try:
                LicenseService.cancel_license(license)
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
            'Renew a license. Body `{"requested_end": "<iso-8601>"}`. '
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

        # IP-008 Phase 3 C7: pass the exact decision datetime through;
        # `LicenseService.renew_license` now accepts a `new_end_date`
        # so the LSD-required sub-day precision is preserved.
        try:
            LicenseService.renew_license(license_obj, new_end_date=decision.new_end)
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
