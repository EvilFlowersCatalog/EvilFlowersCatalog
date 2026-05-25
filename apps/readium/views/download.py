"""
License Gateway: serves `.lcpl` files to reading apps.

IP-009 Phase 4 (Q3 resolution): this view is **capability-token only**.
Bearer JWT and `?access_token=…` are rejected with 401 + a problem
detail. Tokens are minted inline by `LicenseSerializer.Base.download_url`
whenever a serializer carries a request context — so the client opens
the URL straight from the License response, no separate mint round-trip.

Two scopes resolve here:

  - `lcpl_download` — single-use; consumed atomically. Used by direct
    UI downloads.
  - `lcpl_feed_download` — multi-use peek; OPDS feed serialization
    embeds the URL so reading apps can fetch the `.lcpl` repeatedly
    within the feed-render TTL.

The token's `resource_id` must equal the URL's `license_id` —
prevents cross-license replay.
"""

from http import HTTPStatus
from uuid import UUID

from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views import View

from apps import openapi
from apps.core.errors import ProblemDetailException, DetailType
from apps.core.services.capability_tokens import CapabilityTokenService
from apps.readium.capability_scopes import LCPL_DOWNLOAD, LCPL_FEED_DOWNLOAD
from apps.readium.models import License
from apps.readium.services import LicenseService


class LicenseDownloadView(View):
    @openapi.metadata(
        description=(
            "Download the LCP license file (`.lcpl`) for a specific license. "
            "**Capability-token only** after IP-009 Phase 4. The token is "
            "embedded in the `download_url` field of any `License` response "
            "or OPDS feed entry — append it as `?token=…`. Bearer / "
            "`?access_token=…` is rejected with 401."
        ),
        tags=["Licenses"],
        summary="Download LCP license file",
    )
    def get(self, request, license_id: UUID):
        token = request.GET.get("token")
        if not token:
            raise ProblemDetailException(
                _("Capability token required"),
                detail=_(
                    "Fetch the License (Bearer-authenticated) and open the `download_url` field, which "
                    "carries a fresh single-use token."
                ),
                status=HTTPStatus.UNAUTHORIZED,
                detail_type=DetailType.FORBIDDEN,
            )

        # Try single-use scope first (the canonical UI path). If the
        # token doesn't resolve under it, fall back to the multi-use
        # feed scope. Either way the payload's `scope` matches.
        payload = CapabilityTokenService.consume(token, expected_scope=LCPL_DOWNLOAD)
        if payload is None:
            payload = CapabilityTokenService.peek(token, expected_scope=LCPL_FEED_DOWNLOAD)
        if payload is None:
            raise ProblemDetailException(
                _("Invalid or expired capability token"),
                detail=_("Re-fetch the License to obtain a fresh `download_url`."),
                status=HTTPStatus.UNAUTHORIZED,
                detail_type=DetailType.FORBIDDEN,
            )

        if payload.get("resource_id") != str(license_id):
            raise ProblemDetailException(
                _("Capability token does not authorise this license"),
                status=HTTPStatus.UNAUTHORIZED,
                detail_type=DetailType.FORBIDDEN,
            )

        try:
            license = License.objects.get(pk=license_id)
        except License.DoesNotExist as e:
            raise ProblemDetailException(
                _("License not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not license.lcp_license_id:
            raise ProblemDetailException(
                _("License not yet generated"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

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

        try:
            fresh_license = LicenseService.fetch_fresh_license(license)
            response = JsonResponse(fresh_license, content_type="application/vnd.readium.lcp.license.v1.0+json")
            response["Content-Disposition"] = f'attachment; filename="{license.entry.title}.lcpl"'
            return response

        except ValueError as e:
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
