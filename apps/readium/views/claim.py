"""
EFC-hosted reservation claim page (IP-011 Phase 1, G1 closeout).

The `reservation_available` email links here. The GET endpoint renders a
minimal Django template showing the entry title/author and a confirmation
button. The button is a form that POSTs back to the same path with the
scoped JWT carried as a hidden field. The POST endpoint calls
`ReservationService.claim` and renders a success/error page.

Why two endpoints sharing a path:

- GET-only mutation is a REST/security antipattern — link prefetchers
  (Outlook, Apple Mail, anti-phishing scanners) would routinely claim
  reservations against the user's will.
- A separate confirmation form GET + state-mutating POST mirrors the
  IP-003 "PATCH-only mutation" contract while keeping email links
  clickable without depending on a separate frontend.

When `EVILFLOWERS_PORTAL_URL` is set, the GET 302-redirects clicked-from-email
links to elvira-portal so deployments that prefer the SPA UX get a polished
claim screen. When the setting is unset (default), EFC serves the page
itself so the catalog remains standalone-usable.

Scope/replay protection:

- Token must carry `scope == "reservation:claim"` and the embedded `sub`
  must match the reservation's owner.
- After a successful claim the token's `jti` is written to a Redis
  deny-list with the token's remaining TTL — a replay returns 410 Gone.
"""

from datetime import datetime, timezone as dt_timezone
from http import HTTPStatus
from urllib.parse import quote
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from joserfc.errors import JoseError

from apps.core.auth import JWTFactory
from apps.core.errors import DetailType, ProblemDetailException
from apps.readium.models import Reservation
from apps.readium.services import PassphraseRequiredError, ReservationService

CLAIM_SCOPE = "reservation:claim"
USED_TOKEN_CACHE_PREFIX = "readium:scoped-token-used:"


def _decode_claim_token(token: str, reservation: Reservation) -> dict:
    """Decode a scoped JWT and assert it authorises a claim of `reservation`.

    Raises a ProblemDetailException if the token is invalid, has the wrong
    scope, has already been consumed, or belongs to a different user.
    """
    try:
        claims = JWTFactory.decode(token)
    except JoseError as exc:
        raise ProblemDetailException(
            _("Invalid or expired claim token"),
            status=HTTPStatus.UNAUTHORIZED,
            detail_type=DetailType.FORBIDDEN,
            previous=exc,
        )

    if claims.get("type") != "scoped" or claims.get("scope") != CLAIM_SCOPE:
        raise ProblemDetailException(
            _("Token is not authorised for reservation claim"),
            status=HTTPStatus.UNAUTHORIZED,
            detail_type=DetailType.FORBIDDEN,
        )

    if str(claims.get("sub")) != str(reservation.user_id):
        raise ProblemDetailException(
            _("Token does not authorise this reservation"),
            status=HTTPStatus.FORBIDDEN,
            detail_type=DetailType.FORBIDDEN,
        )

    jti = claims.get("jti")
    if jti and cache.get(f"{USED_TOKEN_CACHE_PREFIX}{jti}"):
        raise ProblemDetailException(
            _("This claim link has already been used"),
            status=HTTPStatus.GONE,
            detail_type=DetailType.FORBIDDEN,
        )

    return claims


def _consume_token(claims: dict) -> None:
    """Mark a scoped JWT's `jti` as used in the deny-list."""
    jti = claims.get("jti")
    if not jti:
        return
    exp = claims.get("exp")
    if isinstance(exp, datetime):
        ttl = int((exp - datetime.now(dt_timezone.utc)).total_seconds())
    else:
        try:
            ttl = int(int(exp) - datetime.now(dt_timezone.utc).timestamp())
        except (TypeError, ValueError):
            ttl = settings.EVILFLOWERS_NOTIFICATION_SCOPED_TOKEN_TTL_HOURS * 3600
    if ttl <= 0:
        return
    cache.set(f"{USED_TOKEN_CACHE_PREFIX}{jti}", 1, timeout=ttl)


def _get_reservation(reservation_id: UUID) -> Reservation:
    try:
        return Reservation.objects.select_related("entry", "user").get(pk=reservation_id)
    except Reservation.DoesNotExist as exc:
        raise ProblemDetailException(
            _("Reservation not found"),
            status=HTTPStatus.NOT_FOUND,
            previous=exc,
            detail_type=DetailType.NOT_FOUND,
        )


@method_decorator(csrf_exempt, name="dispatch")
class ReservationClaimPage(View):
    """GET — render the claim confirmation form (or redirect to portal).
    POST — perform the claim.

    Email links land here. When `EVILFLOWERS_PORTAL_URL` is set, the GET
    branch 302-redirects to the SPA equivalent instead of rendering.
    """

    def get(self, request, reservation_id: UUID):
        token = request.GET.get("access_token", "").strip()
        if not token:
            raise ProblemDetailException(
                _("Missing claim token"),
                status=HTTPStatus.UNAUTHORIZED,
                detail_type=DetailType.FORBIDDEN,
            )

        portal_url = getattr(settings, "EVILFLOWERS_PORTAL_URL", "")
        if portal_url:
            target = (
                f"{portal_url.rstrip('/')}/library/reservations/{reservation_id}/claim" f"?access_token={quote(token)}"
            )
            return HttpResponseRedirect(target)

        reservation = _get_reservation(reservation_id)
        _decode_claim_token(token, reservation)

        html = render_to_string(
            "readium/claim_confirm.html",
            {
                "reservation": reservation,
                "entry": reservation.entry,
                "claim_deadline": reservation.claim_deadline,
                "access_token": token,
                "post_url": f"/readium/v1/reservations/{reservation.pk}/claim",
            },
        )
        return HttpResponse(html, content_type="text/html")

    def post(self, request, reservation_id: UUID):
        token = (request.POST.get("access_token") or request.GET.get("access_token") or "").strip()
        if not token:
            raise ProblemDetailException(
                _("Missing claim token"),
                status=HTTPStatus.UNAUTHORIZED,
                detail_type=DetailType.FORBIDDEN,
            )

        reservation = _get_reservation(reservation_id)
        claims = _decode_claim_token(token, reservation)

        try:
            license_obj = ReservationService.claim(reservation)
        except PassphraseRequiredError as exc:
            html = render_to_string(
                "readium/claim_result.html",
                {
                    "success": False,
                    "reservation": reservation,
                    "entry": reservation.entry,
                    "error_title": _("LCP passphrase required"),
                    "error_detail": str(exc),
                    "set_passphrase_url": "/api/v1/users/me",
                },
            )
            return HttpResponse(html, content_type="text/html", status=HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            html = render_to_string(
                "readium/claim_result.html",
                {
                    "success": False,
                    "reservation": reservation,
                    "entry": reservation.entry,
                    "error_title": _("Cannot claim reservation"),
                    "error_detail": str(exc),
                },
            )
            return HttpResponse(html, content_type="text/html", status=HTTPStatus.CONFLICT)

        _consume_token(claims)

        html = render_to_string(
            "readium/claim_result.html",
            {
                "success": True,
                "reservation": reservation,
                "entry": reservation.entry,
                "license": license_obj,
            },
        )
        return HttpResponse(html, content_type="text/html", status=HTTPStatus.OK)
