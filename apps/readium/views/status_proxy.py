"""
LSD (License Status Document) Proxy

Full proxy for the LCP Status Server. LSD is never exposed publicly.
All reading app interactions with the Status Server go through these endpoints.
Link URLs in responses are rewritten to point back to our proxy.
"""

import requests as http_requests
from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.views import SecuredView
from apps.readium.models import License


class StatusProxyView(SecuredView):
    """Base for all LSD proxy endpoints."""

    def _get_lsd_url(self) -> str:
        return getattr(settings, "EVILFLOWERS_READIUM_LSDSV_URL", "http://127.0.0.1:8990")

    def _get_license(self, license_id: UUID) -> License:
        try:
            return License.objects.get(pk=license_id)
        except License.DoesNotExist:
            raise ProblemDetailException(_("License not found"), status=HTTPStatus.NOT_FOUND)

    def _rewrite_links(self, data: dict, request, license_id: UUID) -> dict:
        """Rewrite LSD links to point through our proxy."""
        base_url = f"{request.scheme}://{request.get_host()}"
        if "links" in data and isinstance(data["links"], list):
            for link in data["links"]:
                rel = link.get("rel", "")
                if rel == "register" or rel == "license":
                    link["href"] = f"{base_url}{reverse('readium:lsd-register', kwargs={'license_id': license_id})}"
                elif rel == "return":
                    link["href"] = f"{base_url}{reverse('readium:lsd-return', kwargs={'license_id': license_id})}"
                elif rel == "renew":
                    link["href"] = f"{base_url}{reverse('readium:lsd-renew', kwargs={'license_id': license_id})}"
                elif rel == "hint":
                    link["href"] = f"{base_url}{reverse('readium:hint')}"
        return data


class StatusDocumentView(StatusProxyView):
    """GET /readium/v1/licenses/{license_id}/status -- License Status Document."""

    def get(self, request, license_id: UUID):
        license_obj = self._get_license(license_id)
        if not license_obj.lcp_license_id:
            raise ProblemDetailException(_("License not yet generated"), status=HTTPStatus.NOT_FOUND)

        try:
            response = http_requests.get(
                f"{self._get_lsd_url()}/licenses/{license_obj.lcp_license_id}/status",
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(
                _("Failed to fetch license status"), status=HTTPStatus.BAD_GATEWAY, previous=e
            )

        data = self._rewrite_links(data, request, license_id)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")


class DeviceRegistrationProxyView(StatusProxyView):
    """POST /readium/v1/licenses/{license_id}/register -- Device registration."""

    def post(self, request, license_id: UUID):
        license_obj = self._get_license(license_id)
        if not license_obj.lcp_license_id:
            raise ProblemDetailException(_("License not yet generated"), status=HTTPStatus.NOT_FOUND)

        device_id = request.GET.get("id", "")
        device_name = request.GET.get("name", "")

        try:
            response = http_requests.post(
                f"{self._get_lsd_url()}/licenses/{license_obj.lcp_license_id}/register",
                params={"id": device_id, "name": device_name},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(_("Failed to register device"), status=HTTPStatus.BAD_GATEWAY, previous=e)

        # Update local device count
        license_obj.device_count += 1
        if license_obj.state == License.LicenseState.READY:
            license_obj.state = License.LicenseState.ACTIVE
        license_obj.save()

        data = self._rewrite_links(data, request, license_id)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")


class ReturnProxyView(StatusProxyView):
    """PUT /readium/v1/licenses/{license_id}/return -- Loan return."""

    def put(self, request, license_id: UUID):
        license_obj = self._get_license(license_id)
        if not license_obj.lcp_license_id:
            raise ProblemDetailException(_("License not yet generated"), status=HTTPStatus.NOT_FOUND)

        device_id = request.GET.get("id", "")
        device_name = request.GET.get("name", "")

        try:
            response = http_requests.put(
                f"{self._get_lsd_url()}/licenses/{license_obj.lcp_license_id}/return",
                params={"id": device_id, "name": device_name},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(_("Failed to return loan"), status=HTTPStatus.BAD_GATEWAY, previous=e)

        # Update local state
        from apps.readium.services import StatusServerClient

        StatusServerClient().return_license(license_obj)

        data = self._rewrite_links(data, request, license_id)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")


class RenewProxyView(StatusProxyView):
    """PUT /readium/v1/licenses/{license_id}/renew -- Loan renewal."""

    def put(self, request, license_id: UUID):
        license_obj = self._get_license(license_id)
        if not license_obj.lcp_license_id:
            raise ProblemDetailException(_("License not yet generated"), status=HTTPStatus.NOT_FOUND)

        device_id = request.GET.get("id", "")
        device_name = request.GET.get("name", "")
        end = request.GET.get("end", "")

        params = {"id": device_id, "name": device_name}
        if end:
            params["end"] = end

        try:
            response = http_requests.put(
                f"{self._get_lsd_url()}/licenses/{license_obj.lcp_license_id}/renew",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(_("Failed to renew loan"), status=HTTPStatus.BAD_GATEWAY, previous=e)

        data = self._rewrite_links(data, request, license_id)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")
