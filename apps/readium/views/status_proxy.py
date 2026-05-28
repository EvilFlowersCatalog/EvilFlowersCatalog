"""
LSD (License Status Document) Proxy

Full proxy for the LCP Status Server. LSD is never exposed publicly.
All reading app interactions with the Status Server go through these endpoints.
Link URLs in responses are rewritten to point back to our proxy.
"""

import logging

import requests as http_requests
from http import HTTPStatus
from urllib.parse import urlparse
from uuid import UUID

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.views import SecuredView
from apps.readium.models import License
from apps.readium.services.lsd_transport import _format_error, _split_url_and_auth

logger = logging.getLogger(__name__)


class StatusProxyView(SecuredView):
    """Base for all LSD proxy endpoints."""

    def _get_lsd(self):
        """Return (clean_url, auth_tuple_or_None) for the upstream LSD.

        Strips URL-embedded credentials so the rewritten link targets
        we render don't leak userinfo and so error logs carry a redacted
        URL. `requests` would extract them anyway, but doing it here lets
        us also reuse the clean URL for `_internal_lsd_host` comparison.
        """
        raw = getattr(settings, "EVILFLOWERS_READIUM_LSDSV_URL", "http://127.0.0.1:8990")
        return _split_url_and_auth(raw)

    def _get_lsd_url(self) -> str:
        return self._get_lsd()[0]

    def _get_license(self, license_id: UUID) -> License:
        try:
            return License.objects.get(pk=license_id)
        except License.DoesNotExist:
            raise ProblemDetailException(_("License not found"), status=HTTPStatus.NOT_FOUND)

    def _internal_lsd_host(self) -> str:
        """Hostname (with port) of the upstream LSD as seen from the catalog.

        Used to detect leaked internal URLs in LSD responses so we can
        rewrite ANY link targeting that host, not only the rels we
        special-case below (IP-008 Phase 2 B2).
        """
        return urlparse(self._get_lsd_url()).netloc

    def _rewrite_links(self, data: dict, request, license_id: UUID) -> dict:
        """Rewrite LSD links to point through our proxy.

        IP-008 Phase 2 B2: host-based AND rel-based. We map the known
        rels to our proxy URLs (existing behaviour); additionally, ANY
        remaining link whose host matches the internal LSD host gets a
        path-only rewrite so we don't leak `127.0.0.1:8990` to reader
        apps via `status`, `publication`, `self`, etc.
        """
        base_url = f"{request.scheme}://{request.get_host()}"
        if "links" in data and isinstance(data["links"], list):
            internal_host = self._internal_lsd_host()
            for link in data["links"]:
                rel = link.get("rel", "")
                if rel == "register" or rel == "license":
                    link["href"] = f"{base_url}{reverse('readium:lsd-register', kwargs={'license_id': license_id})}"
                    continue
                if rel == "return":
                    link["href"] = f"{base_url}{reverse('readium:lsd-return', kwargs={'license_id': license_id})}"
                    continue
                if rel == "renew":
                    link["href"] = f"{base_url}{reverse('readium:lsd-renew', kwargs={'license_id': license_id})}"
                    continue
                if rel == "hint":
                    link["href"] = f"{base_url}{reverse('readium:hint')}"
                    continue

                href = link.get("href")
                if not href:
                    continue
                parsed = urlparse(href)
                if parsed.netloc == internal_host:
                    # Strip the internal scheme+host so the URL is
                    # served from our public origin (which proxies
                    # everything LSD-related anyway).
                    rewritten_path = parsed.path
                    if parsed.query:
                        rewritten_path = f"{rewritten_path}?{parsed.query}"
                    link["href"] = f"{base_url}{rewritten_path}"
        return data


class StatusDocumentView(StatusProxyView):
    """GET /readium/v1/licenses/{license_id}/status -- License Status Document."""

    def get(self, request, license_id: UUID):
        license_obj = self._get_license(license_id)
        if not license_obj.lcp_license_id:
            raise ProblemDetailException(_("License not yet generated"), status=HTTPStatus.NOT_FOUND)

        lsd_url, lsd_auth = self._get_lsd()
        try:
            response = http_requests.get(
                f"{lsd_url}/licenses/{license_obj.lcp_license_id}/status",
                timeout=30,
                auth=lsd_auth,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(
                _("Failed to fetch license status"),
                detail=_format_error("LSD get_status (proxy)", e),
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
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

        lsd_url, lsd_auth = self._get_lsd()
        try:
            response = http_requests.post(
                f"{lsd_url}/licenses/{license_obj.lcp_license_id}/register",
                params={"id": device_id, "name": device_name},
                timeout=30,
                auth=lsd_auth,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(
                _("Failed to register device"),
                detail=_format_error("LSD register_device (proxy)", e),
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
            )

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

        # IP-008 Phase 3 C6: forward the return to LSD (canonical state
        # surface), then mirror the resulting state locally as a cache.
        # We do NOT call `StatusServerClient.return_license` here because
        # that would PATCH the Status Server a second time — the LSD PUT
        # below is the canonical state change.
        lsd_url, lsd_auth = self._get_lsd()
        try:
            response = http_requests.put(
                f"{lsd_url}/licenses/{license_obj.lcp_license_id}/return",
                params={"id": device_id, "name": device_name},
                timeout=30,
                auth=lsd_auth,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(
                _("Failed to return loan"),
                detail=_format_error("LSD return (proxy)", e),
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
            )

        # IP-009 Phase 3 C2: the LSD PUT above is the canonical state
        # change. Reconcile the local row from the proxied response so
        # we don't GET twice. Falls back to a refetch if the response
        # body doesn't carry a parseable status doc.
        from django.db import transaction

        from apps.readium.services import LicenseService, StatusServerSyncService
        from apps.readium.services.lcp_server_client import LCPServerClient

        with transaction.atomic():
            license_obj = License.objects.select_for_update().get(pk=license_id)
            StatusServerSyncService().reconcile(license_obj, lsd_doc=data if isinstance(data, dict) else None)
            try:
                LCPServerClient().update_license_rights(license_obj)
            except Exception:
                # LCP rights update is best-effort; LSD already considers
                # the loan returned. Logging only.
                import logging

                logging.getLogger(__name__).exception(
                    "LCP rights update failed after LSD return for license %s", license_obj.pk
                )

        # Best-effort queue promotion (matches LicenseService.return_license).
        LicenseService._maybe_promote_next(license_obj)

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

        lsd_url, lsd_auth = self._get_lsd()
        try:
            response = http_requests.put(
                f"{lsd_url}/licenses/{license_obj.lcp_license_id}/renew",
                params=params,
                timeout=30,
                auth=lsd_auth,
            )
            response.raise_for_status()
            data = response.json()
        except http_requests.RequestException as e:
            raise ProblemDetailException(
                _("Failed to renew loan"),
                detail=_format_error("LSD renew (proxy)", e),
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
            )

        data = self._rewrite_links(data, request, license_id)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")
