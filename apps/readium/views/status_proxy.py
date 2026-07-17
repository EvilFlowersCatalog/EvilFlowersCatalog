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
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from apps.core.errors import DetailType, ProblemDetailException
from apps.core.services.capability_tokens import CapabilityTokenService
from apps.core.views import SecuredView
from apps.readium.capability_scopes import LCPL_FEED_DOWNLOAD, lcpl_feed_download_ttl
from apps.readium.models import License
from apps.readium.services.lsd_transport import _format_error, _split_url_and_auth
from apps.readium.services.renew_policy import evaluate_renew
from apps.readium.views._license_lookup import resolve_license

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
        # Accepts our pk *or* the LCP license id: the `status` link inside a
        # signed `.lcpl` is expanded by the LCP server with the latter.
        license_obj = resolve_license(license_id)
        if license_obj is None:
            raise ProblemDetailException(_("License not found"), status=HTTPStatus.NOT_FOUND)
        return license_obj

    def _internal_lsd_host(self) -> str:
        """Hostname (with port) of the upstream LSD as seen from the catalog.

        Used to detect leaked internal URLs in LSD responses so we can
        rewrite ANY link targeting that host, not only the rels we
        special-case below (IP-008 Phase 2 B2).
        """
        return urlparse(self._get_lsd_url()).netloc

    def _license_gateway_url(self, base_url: str, license_obj: License) -> str:
        """Tokenised `.lcpl` gateway URL for the LSD `license` rel.

        The reader re-downloads an updated License Document from this link
        after a renew/return (LSD `updated.license` timestamp check). The
        gateway is capability-token only (`views/download.py`), and a reader
        arriving from a public LSD endpoint carries no bearer JWT — so we mint
        an `lcpl_feed_download` token (multi-use peek within TTL) bound to the
        license, exactly as OPDS feeds do via `BorrowLinkResolver.license_url`.
        A fresh token is minted every time the status document is fetched, so
        the reader always re-derives a live URL from the latest status doc.
        """
        path = reverse("readium:license-gateway", kwargs={"license_id": license_obj.pk})
        token = CapabilityTokenService.mint(
            scope=LCPL_FEED_DOWNLOAD,
            subject={"sub": str(license_obj.user_id), "resource_id": str(license_obj.pk)},
            ttl=lcpl_feed_download_ttl(),
            single_use=False,
        )
        return f"{base_url}{path}?token={token}"

    def _rewrite_links(self, data: dict, request, license_obj: License) -> dict:
        """Rewrite LSD links to point through our proxy.

        IP-008 Phase 2 B2: host-based AND rel-based. We map the known
        rels to our proxy URLs (existing behaviour); additionally, ANY
        remaining link whose host matches the internal LSD host gets a
        path-only rewrite so we don't leak `127.0.0.1:8990` to reader
        apps via `status`, `publication`, `self`, etc.
        """
        base_url = f"{request.scheme}://{request.get_host()}"
        # Always reverse our proxy routes with our own pk: `license_obj` was
        # resolved from either our pk or the LCP license id, and the
        # `.lcpl` gateway looks the row up strictly by `pk`.
        license_id = license_obj.pk
        if "links" in data and isinstance(data["links"], list):
            internal_host = self._internal_lsd_host()
            for link in data["links"]:
                rel = link.get("rel", "")
                if rel == "register":
                    link["href"] = f"{base_url}{reverse('readium:lsd-register', kwargs={'license_id': license_id})}"
                    continue
                if rel == "license":
                    # The License Document link — NOT the device-register
                    # endpoint. Points at the tokenised `.lcpl` gateway so the
                    # reader can fetch the refreshed license after renew/return.
                    link["href"] = self._license_gateway_url(base_url, license_obj)
                    continue
                if rel == "return":
                    link["href"] = f"{base_url}{reverse('readium:lsd-return', kwargs={'license_id': license_id})}"
                    continue
                if rel == "renew":
                    link["href"] = f"{base_url}{reverse('readium:lsd-renew', kwargs={'license_id': license_id})}"
                    continue
                if rel == "hint":
                    # Carry the license through: without it the page can only
                    # render the generic fallback hint, never the per-license one.
                    link["href"] = f"{base_url}{reverse('readium:hint')}?license_id={license_id}"
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

        data = self._rewrite_links(data, request, license_obj)
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

        data = self._rewrite_links(data, request, license_obj)
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
            # Re-fetch by the resolved pk, NOT the URL kwarg: links inside a
            # signed `.lcpl` carry the LCP license id, so `license_id` here is
            # frequently not our pk and this lookup would 500 on the reader path.
            license_obj = License.objects.select_for_update().get(pk=license_obj.pk)
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

        data = self._rewrite_links(data, request, license_obj)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")


class RenewProxyView(StatusProxyView):
    """PUT /readium/v1/licenses/{license_id}/renew -- Loan renewal.

    This is the door reading apps come through: the `renew` link inside the
    LSD status document points here. It used to forward straight to the Status
    Server, applying none of the STU renewal policy — so a reader could renew
    while other users were queued for the title, inside the acquisition
    embargo, or past the per-loan renewal cap, and `renewal_count` was never
    incremented (which meant the cap could never be reached on the portal path
    either). The bypass was unreachable only because the status document
    carried no `renew` link until `license_status.renew` was enabled.

    Policy lives in `evaluate_renew` and the transition in
    `LicenseService.renew_license` — the same two the portal's
    `PUT /readium/v1/licenses/{id}` uses — so both doors now behave identically.
    """

    def put(self, request, license_id: UUID):
        license_obj = self._get_license(license_id)
        if not license_obj.lcp_license_id:
            raise ProblemDetailException(_("License not yet generated"), status=HTTPStatus.NOT_FOUND)

        end = request.GET.get("end", "")
        requested_end = parse_datetime(end) if end else None
        if end and requested_end is None:
            raise ProblemDetailException(
                _("`end` must be an ISO-8601 datetime"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        decision = evaluate_renew(license_obj, requested_end=requested_end)
        if not decision.allowed:
            raise ProblemDetailException(
                _("Renewal denied"),
                detail=decision.reason,
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.CONFLICT,
            )

        from apps.readium.services import LicenseService

        try:
            LicenseService.renew_license(license_obj, new_end_date=decision.new_end)
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to renew loan"),
                detail=str(e),
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
            )

        # The LSD contract expects the updated status document back.
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
                detail=_format_error("LSD get_status (renew proxy)", e),
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
            )

        data = self._rewrite_links(data, request, license_obj)
        return JsonResponse(data, content_type="application/vnd.readium.license.status.v1.0+json")
