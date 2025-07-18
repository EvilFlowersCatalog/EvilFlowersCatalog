from http import HTTPStatus
from uuid import UUID

from django.http import JsonResponse
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.core.errors import ProblemDetailException, DetailType
from apps.core.views import SecuredView
from apps.readium.models import License
from apps.readium.lcp_client import LCPClient


class LicenseDownloadView(SecuredView):
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
        description="Download the LCP license file for a specific license. Returns the actual LCP license JSON that can be imported into reading applications.",
        tags=["Licenses"],
        summary="Download LCP license file",
    )
    def get(self, request, license_id: UUID):
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
