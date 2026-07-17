"""
Declarative Reservation API (IP-003 Phase 3).

Endpoints:

    POST   /readium/v1/reservations           -- create (queue user for an entry)
    GET    /readium/v1/reservations           -- list, with filters
    GET    /readium/v1/reservations/{id}      -- detail
    PATCH  /readium/v1/reservations/{id}      -- mutate status

The only client-callable status transitions are `cancelled` and `claimed`.
Server-side transitions (`queued -> available`, `available -> expired`)
are NOT exposed as endpoints — they happen as side effects of license
state changes and the Celery sweep.
"""

from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import PaginationResponse, SingleResponse
from apps.core.errors import DetailType, ProblemDetailException, ValidationException
from apps.core.views import SecuredView
from apps.readium.filters import ReservationFilter
from apps.readium.forms import CreateReservationForm, UpdateReservationForm
from apps.readium.models import Reservation
from apps.readium.serializers import LicenseSerializer, ReservationSerializer
from apps.readium.services import LicenseService, PassphraseRequiredError, ReservationService


class ReservationCollection(SecuredView):
    @openapi.metadata(
        description="List the caller's reservations. Superusers see all. "
        "Supports filters: entry_id, user_id, status (comma-separated for OR).",
        tags=["Reservations"],
        summary="List reservations",
    )
    def get(self, request):
        qs = ReservationFilter(request.GET, queryset=Reservation.objects.all(), request=request).qs
        return PaginationResponse(request, qs, serializer=ReservationSerializer.Base)

    @openapi.metadata(
        description=(
            "Place a reservation on a fully-borrowed entry. "
            'The body is `{"entry_id": "<uuid>"}`. Returns 409 if the user already has '
            "an active license or a non-terminal reservation for this entry."
        ),
        tags=["Reservations"],
        summary="Create reservation",
    )
    def post(self, request):
        form = CreateReservationForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        try:
            reservation = ReservationService.enqueue(
                entry=form.cleaned_data["entry_id"],
                user=request.user,
            )
        except ValueError as e:
            raise ProblemDetailException(
                str(e),
                status=HTTPStatus.CONFLICT,
                detail_type=DetailType.CONFLICT,
                previous=e,
            )

        return SingleResponse(
            request,
            data=ReservationSerializer.Base.model_validate(reservation),
            status=HTTPStatus.CREATED,
        )


class ReservationDetail(SecuredView):
    @staticmethod
    def _get_reservation(request, reservation_id: UUID) -> Reservation:
        try:
            reservation = Reservation.objects.get(pk=reservation_id)
        except Reservation.DoesNotExist as e:
            raise ProblemDetailException(
                _("Reservation not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not (request.user.is_superuser or reservation.user_id == request.user.pk):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return reservation

    @openapi.metadata(
        description="Read a reservation. Callers only see their own; superusers see any.",
        tags=["Reservations"],
        summary="Get reservation",
    )
    def get(self, request, reservation_id: UUID):
        reservation = self._get_reservation(request, reservation_id)
        return SingleResponse(request, data=ReservationSerializer.Base.model_validate(reservation))

    @openapi.metadata(
        description=(
            'Mutate a reservation\'s status. Body: `{"status": "cancelled"}` (user-cancel) '
            'or `{"status": "claimed"}` (convert an `available` reservation into a license). '
            "Server-side transitions (`queued`→`available`, `available`→`expired`) are not exposed."
        ),
        tags=["Reservations"],
        summary="Update reservation status",
    )
    def patch(self, request, reservation_id: UUID):
        reservation = self._get_reservation(request, reservation_id)
        form = UpdateReservationForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        new_status = form.cleaned_data["status"]

        if new_status == Reservation.Status.CANCELLED:
            try:
                ReservationService.cancel(reservation)
            except ValueError as e:
                raise ProblemDetailException(
                    str(e),
                    status=HTTPStatus.CONFLICT,
                    detail_type=DetailType.CONFLICT,
                    previous=e,
                )
            return SingleResponse(request, data=ReservationSerializer.Base.model_validate(reservation))

        if new_status == Reservation.Status.CLAIMED:
            try:
                license_obj = ReservationService.claim(reservation)
            except PassphraseRequiredError as e:
                raise ProblemDetailException(
                    _("LCP passphrase required"),
                    detail=str(e),
                    status=HTTPStatus.BAD_REQUEST,
                    detail_type=DetailType.PASSPHRASE_REQUIRED,
                    additional_data={"set_passphrase_url": "/api/v1/users/me"},
                    previous=e,
                )
            except ValueError as e:
                raise ProblemDetailException(
                    str(e),
                    status=HTTPStatus.CONFLICT,
                    detail_type=DetailType.CONFLICT,
                    previous=e,
                )
            response = SingleResponse(
                request,
                data=LicenseSerializer.Base.model_validate(license_obj, context={"request": request}),
                status=HTTPStatus.CREATED,
            )
            response["Location"] = f"/readium/v1/licenses/{license_obj.pk}"
            return response

        # Form choices should make this unreachable, but stay defensive.
        raise ProblemDetailException(_("Unsupported status transition"), status=HTTPStatus.BAD_REQUEST)
