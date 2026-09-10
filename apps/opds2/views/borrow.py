from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.utils.translation import gettext as _

from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException
from apps.core.models import Entry
from apps.opds2.services import ManifestBuilder
from apps.opds2.schema.rwpm import Link
from apps.opds2.views.base import Opds2CatalogView
from apps.readium.models import License
from apps.readium.services import LicenseService, PassphraseRequiredError
from apps.readium.services.renew_policy import evaluate_renew


class BorrowView(Opds2CatalogView):
    def post(self, request, catalog_name: str, entry_id: UUID):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        try:
            entry = Entry.objects.get(pk=entry_id, catalog=self.catalog)
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Publication not found"), status=HTTPStatus.NOT_FOUND)

        if not entry.read_config("readium_enabled"):
            raise ProblemDetailException(
                _("Publication is not available for borrowing"), status=HTTPStatus.BAD_REQUEST
            )

        duration_days = getattr(settings, "EVILFLOWERS_READIUM_DEFAULT_BORROW_DURATION_DAYS", 14)

        # IP-008 Phase 3 D3: drop the duplicated passphrase pre-check.
        # `LicenseService.create_license` already raises
        # `PassphraseRequiredError` when the user has no stored passphrase;
        # we map it to the same RFC 7807 response below. Trusting the
        # service keeps a single source of truth for the rule.
        try:
            license_obj = LicenseService.create_license(
                entry=entry,
                user=request.user,
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

        # IP-008 Phase 2 B5 / Q4: emit the LCP-license link via the shared
        # `BorrowLinkResolver` so OPDS 1.2 and OPDS 2.0 stay aligned on
        # (rel, type, href) for the same entry.
        from apps.opds.services.borrow_link import BorrowLinkResolver

        resolver = BorrowLinkResolver(request, opds_version="2.0")
        # We just minted `license_obj`, so the borrow flow caller is the
        # active-license owner — pass it directly rather than re-querying.
        for borrow_link in resolver.emit_links(entry, active_license=license_obj):
            link = Link(href=borrow_link.href, type=borrow_link.type, rel=borrow_link.rel)
            if publication.links:
                publication.links.append(link)
            else:
                publication.links = [link]

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

        # Route through the same renewal policy as the portal PUT and the LSD
        # `renew` proxy (test_renew_policy_parity). Skipping `evaluate_renew`
        # here would let this door renew past the per-loan cap, inside the
        # acquisition embargo, or while other users are queued for the title.
        decision = evaluate_renew(license_obj, requested_end=None)
        if not decision.allowed:
            raise ProblemDetailException(
                _("Renewal denied"),
                detail=decision.reason,
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.CONFLICT,
            )

        try:
            LicenseService.renew_license(license_obj, new_end_date=decision.new_end)
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to renew loan"), status=HTTPStatus.INTERNAL_SERVER_ERROR, previous=e
            )

        return self.opds_response({"status": "renewed"})
