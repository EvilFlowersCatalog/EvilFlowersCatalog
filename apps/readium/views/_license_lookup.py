"""
Shared license-lookup mixin (IP-008 Phase 3 D4).

`LicenseDetail._get_license` and `LicenseDownloadView._get_license` used
to be almost-identical copies differing only in the permission
predicate. The duplication is a maintenance hazard: any change to the
404/403 mapping had to be applied twice. This mixin parameterizes the
predicate via `license_permission` and centralizes the lookup.
"""

from http import HTTPStatus
from typing import Optional
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.core.errors import DetailType, ProblemDetailException
from apps.readium.models import License


def resolve_license(identifier) -> Optional[License]:
    """Resolve a License by catalog pk **or** `lcp_license_id`.

    Links embedded in a `.lcpl` are written by the LCP server before it
    signs the document, and it expands `{license_id}` with the *LCP*
    license id — not our pk. The signature covers the links, so we cannot
    rewrite them after issuance. The public endpoints those links point at
    (LSD proxy, hint page) must therefore accept either identifier.

    Both columns hold unique UUIDs, so an OR lookup is unambiguous in
    practice. Returns None for unknown or malformed identifiers; callers
    decide the error shape.
    """
    try:
        return License.objects.filter(Q(pk=identifier) | Q(lcp_license_id=identifier)).first()
    except (ValidationError, ValueError, TypeError):
        # Malformed UUID — indistinguishable from "not found" to a caller.
        return None


class LicenseLookupMixin:
    """Mixin that resolves a License by id and enforces a named predicate.

    Subclasses set `license_permission` to the `check_*` name registered
    with `object_checker`. Common choices:
      - `check_license_state_manage` (admin state transitions)
      - `check_license_download`     (owner-only `.lcpl` download)
    """

    license_permission: str = "check_license_state_manage"

    def get_license_or_404(self, request, license_id: UUID) -> License:
        try:
            license_obj = License.objects.get(pk=license_id)
        except License.DoesNotExist as e:
            raise ProblemDetailException(
                _("License not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not has_object_permission(self.license_permission, request.user, license_obj):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return license_obj
