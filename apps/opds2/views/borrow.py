from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException
from apps.core.models import Entry
from apps.opds2.services import ManifestBuilder
from apps.opds2.schema.rwpm import Link
from apps.opds2.views.base import Opds2CatalogView
from apps.readium.models import License
from apps.readium.services import LicenseService, PassphraseRequiredError


class BorrowView(Opds2CatalogView):
    def post(self, request, catalog_name: str, entry_id: UUID):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        # Validate passphrase is configured. The service layer also enforces this;
        # we pre-check here so the SPA's borrow flow gets a typed RFC 7807 response
        # without first attempting a write.
        passphrase_hash = getattr(request.user, "lcp_passphrase_hash", None)
        if not passphrase_hash:
            raise ProblemDetailException(
                _("LCP passphrase required"),
                detail=_("Set your LCP passphrase in your profile before borrowing."),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.PASSPHRASE_REQUIRED,
                additional_data={"set_passphrase_url": "/api/v1/users/me"},
            )

        try:
            entry = Entry.objects.get(pk=entry_id, catalog=self.catalog)
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Publication not found"), status=HTTPStatus.NOT_FOUND)

        if not entry.read_config("readium_enabled"):
            raise ProblemDetailException(
                _("Publication is not available for borrowing"), status=HTTPStatus.BAD_REQUEST
            )

        duration_days = getattr(settings, "EVILFLOWERS_READIUM_DEFAULT_BORROW_DURATION_DAYS", 14)

        try:
            license_obj = LicenseService.create_license(
                entry=entry,
                user=request.user,
                passphrase_hash=passphrase_hash,
                passphrase_hint=getattr(request.user, "lcp_passphrase_hint", None) or "Your library password",
                duration_days=duration_days,
            )
        except PassphraseRequiredError as e:
            raise ProblemDetailException(
                _("LCP passphrase required"),
                detail=str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.PASSPHRASE_REQUIRED,
                additional_data={"set_passphrase_url": "/api/v1/users/me"},
            )
        except ValueError as e:
            raise ProblemDetailException(str(e), status=HTTPStatus.CONFLICT)

        base_url = f"{request.scheme}://{request.get_host()}"
        publication = ManifestBuilder.build_publication(entry, base_url=base_url, include_availability=False)

        # Add direct license acquisition link
        license_link = Link(
            href=f"{base_url}{reverse('readium:license-gateway', kwargs={'license_id': license_obj.pk})}",
            type="application/vnd.readium.lcp.license.v1.0+json",
            rel="http://opds-spec.org/acquisition",
        )
        if publication.links:
            publication.links.append(license_link)
        else:
            publication.links = [license_link]

        return self.opds_response(
            publication.model_dump(exclude_none=True, by_alias=True),
            status=HTTPStatus.CREATED,
        )


class ReturnView(Opds2CatalogView):
    def post(self, request, catalog_name: str, entry_id: UUID):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        license_obj = self._get_active_license(request, entry_id)

        try:
            LicenseService.return_license(license_obj)
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to return loan"), status=HTTPStatus.INTERNAL_SERVER_ERROR, previous=e
            )

        return self.opds_response({"status": "returned"})

    def _get_active_license(self, request, entry_id: UUID) -> License:
        try:
            return License.objects.get(
                entry_id=entry_id,
                entry__catalog=self.catalog,
                user=request.user,
                state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            )
        except License.DoesNotExist:
            raise ProblemDetailException(_("No active loan found"), status=HTTPStatus.NOT_FOUND)


class RenewView(Opds2CatalogView):
    def post(self, request, catalog_name: str, entry_id: UUID):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        try:
            license_obj = License.objects.get(
                entry_id=entry_id,
                entry__catalog=self.catalog,
                user=request.user,
                state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            )
        except License.DoesNotExist:
            raise ProblemDetailException(_("No active loan found"), status=HTTPStatus.NOT_FOUND)

        duration_days = getattr(settings, "EVILFLOWERS_READIUM_DEFAULT_BORROW_DURATION_DAYS", 14)

        try:
            LicenseService.renew_license(license_obj, new_duration_days=duration_days)
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to renew loan"), status=HTTPStatus.INTERNAL_SERVER_ERROR, previous=e
            )

        return self.opds_response({"status": "renewed"})
