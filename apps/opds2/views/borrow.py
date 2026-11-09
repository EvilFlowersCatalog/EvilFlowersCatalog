from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.utils.cache import patch_cache_control
from django.utils.translation import gettext as _

from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException
from apps.core.models import Entry
from apps.opds.services.borrow_link import BorrowLinkResolver
from apps.opds2.services import ManifestBuilder
from apps.opds2.views.base import Opds2CatalogView
from apps.readium.models import License, Reservation
from apps.readium.services import LicenseService, PassphraseRequiredError
from apps.readium.services.exceptions import (
    BorrowError,
    NoAvailableSlotsError,
    ReservationError,
    SlotsAvailableError,
)
from apps.readium.services.renew_policy import evaluate_renew
from apps.readium.services.reservation_service import ReservationService


class BorrowView(Opds2CatalogView):
    """The loan resource of an LCP publication.

    `GET` is what reading apps do: OPDS has no verb but GET for following an
    acquisition link, and Thorium dereferences a borrow link typed
    `application/opds-publication+json` with GET. It is idempotent per user —
    the response always describes the user's loan state after the call:

    - a loan exists → the publication with its `.lcpl` acquisition link;
    - a reserved copy is waiting (`ready`) → the reservation is claimed;
    - otherwise a loan is issued, or, when every copy is out, a hold is
      placed (Thorium labels the link "Place hold" when it is
      `unavailable`) and the publication comes back `reserved`;
    - a hold already in the queue is reported, not re-placed.

    `POST` keeps its original contract for the portal: issue a loan or fail
    with `409`.

    Both answer with the OPDS 2.0 publication (`application/opds-publication+json`);
    the borrow link advertises exactly that type, with the LCP license under
    `indirectAcquisition`. The LCP-license link comes from the shared
    `BorrowLinkResolver`, so OPDS 1.2 and 2.0 agree on (rel, type, href).
    """

    def _entry(self, entry_id: UUID) -> Entry:
        try:
            entry = (
                Entry.objects.select_related("catalog", "language")
                .prefetch_related("entry_authors__author", "categories", "acquisitions__encrypted_content")
                .get(pk=entry_id, catalog=self.catalog)
            )
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Publication not found"), status=HTTPStatus.NOT_FOUND)

        if not entry.read_config("readium_enabled"):
            raise ProblemDetailException(
                _("Publication is not available for borrowing"), status=HTTPStatus.BAD_REQUEST
            )
        return entry

    @staticmethod
    def _issue(entry: Entry, user) -> License:
        try:
            return LicenseService.create_license(
                entry=entry,
                user=user,
                duration_days=getattr(settings, "EVILFLOWERS_READIUM_DEFAULT_BORROW_DURATION_DAYS", 14),
            )
        except PassphraseRequiredError as e:
            # `LicenseService.create_license` owns the passphrase rule
            # (IP-008 Phase 3 D3); this only maps it to a problem detail.
            raise ProblemDetailException(
                _("LCP passphrase required"),
                detail=str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.PASSPHRASE_REQUIRED,
                additional_data={"set_passphrase_url": "/api/v1/users/me"},
            )

    @staticmethod
    def _conflict(error: ValueError) -> ProblemDetailException:
        return ProblemDetailException(
            str(error),
            status=HTTPStatus.CONFLICT,
            detail_type=DetailType.CONFLICT,
            additional_data={"reason_code": getattr(error, "reason_code", "borrow_conflict")},
        )

    def _respond(self, request, entry: Entry, status: int = HTTPStatus.OK):
        publication = ManifestBuilder.build_publication(entry, request, user=request.user)
        response = self.publication_response(publication.model_dump(exclude_none=True, by_alias=True), status=status)
        # Per-user loan state carrying a capability-token URL: never cached.
        patch_cache_control(response, private=True, no_store=True)
        return response

    def get(self, request, catalog_name: str, entry_id: UUID):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        entry = self._entry(entry_id)
        state = BorrowLinkResolver.lending_state(entry, request.user)

        if state.license is None:
            reservation = state.reservation
            try:
                if reservation is not None and reservation.status == Reservation.Status.AVAILABLE:
                    ReservationService.claim(reservation)
                elif reservation is None and state.available > 0:
                    try:
                        self._issue(entry, request.user)
                    except NoAvailableSlotsError:
                        # Lost the last copy to a concurrent borrow.
                        ReservationService.enqueue(entry, request.user)
                elif reservation is None:
                    # Every copy is out. Decided here, not from a failed
                    # `create_license`: that checks the passphrase first, and
                    # a hold needs none.
                    try:
                        ReservationService.enqueue(entry, request.user)
                    except SlotsAvailableError:
                        # A copy came back in the meantime.
                        self._issue(entry, request.user)
            except (BorrowError, ReservationError) as e:
                raise self._conflict(e)
            except ValueError as e:
                # Claim-deadline passed, LCP server failure, …
                raise ProblemDetailException(str(e), status=HTTPStatus.CONFLICT, detail_type=DetailType.CONFLICT)

        return self._respond(request, entry)

    def post(self, request, catalog_name: str, entry_id: UUID):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        entry = self._entry(entry_id)
        try:
            self._issue(entry, request.user)
        except BorrowError as e:
            raise self._conflict(e)
        except ValueError as e:
            raise ProblemDetailException(str(e), status=HTTPStatus.CONFLICT)

        return self._respond(request, entry, status=HTTPStatus.CREATED)


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
                additional_data=decision.problem_data,
            )

        try:
            LicenseService.renew_license(license_obj, new_end_date=decision.new_end)
        except Exception as e:
            raise ProblemDetailException(
                _("Failed to renew loan"), status=HTTPStatus.INTERNAL_SERVER_ERROR, previous=e
            )

        return self.opds_response({"status": "renewed"})
