import logging
from http import HTTPStatus
from uuid import UUID

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.user_acquisitions import UserAcquisitionFilter
from apps.api.forms.user_acquisitions import UserAcquisitionForm
from apps.api.response import PaginationResponse, SingleResponse, SeeOtherResponse
from apps.api.serializers.entries import with_entry_graph
from apps.api.serializers.user_acquisitions import UserAcquisitionSerializer
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.models import UserAcquisition, UserActivity
from apps.core.services.activity import ActivityService
from apps.core.views import SecuredView
from apps.openapi.types import ParameterLocation

logger = logging.getLogger(__name__)


class UserAcquisitionManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of user acquisitions across the system. Returns acquisition records that represent user access to specific catalog entries. Supports filtering and pagination to manage large result sets. Includes acquisition details, user information, and associated entry metadata.",
        tags=["User Acquisitions"],
        summary="List all user acquisitions",
    )
    def get(self, request):
        # The Base serializer renders `user`, `acquisition` and a full `entry`
        # (via the `acquisition.entry` property) for every row, so eager-load
        # the whole chain to keep the list from fanning out into an N+1.
        user_acquisitions = with_entry_graph(
            UserAcquisitionFilter(
                request.GET, queryset=UserAcquisition.objects.all(), request=request
            ).qs.select_related("user"),
            "acquisition__entry",
        )

        return PaginationResponse(
            request,
            user_acquisitions,
            serializer=UserAcquisitionSerializer.Base,
            serializer_context={"request": request},
        )

    @openapi.metadata(
        description="Create a new user acquisition record, granting a user access to a specific catalog entry. Supports both personal and shared acquisition types. Validates user permissions for the target entry. A personal request that matches a live grant of the same user, acquisition and page range answers 303 with that grant instead (IP-036); an expired grant is never reused (#215).",
        tags=["User Acquisitions"],
        summary="Create new user acquisition",
    )
    def post(self, request):
        form = UserAcquisitionForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission("check_entry_read", request.user, form.cleaned_data["acquisition_id"].entry):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if form.cleaned_data["type"] == UserAcquisition.UserAcquisitionType.PERSONAL:
            # IP-036: an equivalent live grant (same acquisition and page range) is handed back, never an expired
            # one (#215); another range is another grant. Shared grants are links and always new.
            user_acquisition = UserAcquisition.objects.equivalent(
                request.user,
                form.cleaned_data["acquisition_id"],
                UserAcquisition.UserAcquisitionType.PERSONAL,
                form.cleaned_data.get("range") or None,
            ).first()

            if user_acquisition:
                # Relative on purpose: behind the k8s gateway Django sees http, and an
                # absolute http:// Location is blocked as mixed content on the https portal.
                return SeeOtherResponse(
                    redirect_to=reverse(
                        "api:user-acquisition-detail", kwargs={"user_acquisition_id": user_acquisition.pk}
                    )
                )

        user_acquisition = UserAcquisition(user=request.user)
        form.populate(user_acquisition)

        evilflowers_share_enabled = user_acquisition.acquisition.entry.read_config("evilflowers_share_enabled")
        if user_acquisition.type == UserAcquisition.UserAcquisitionType.SHARED and not evilflowers_share_enabled:
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        user_acquisition.save()

        if user_acquisition.type == UserAcquisition.UserAcquisitionType.SHARED:
            ActivityService.record(
                request.user,
                user_acquisition.acquisition.entry,
                UserActivity.ActivityAction.ACQUISITION_SHARED,
                {"user_acquisition_id": str(user_acquisition.pk), "type": user_acquisition.type},
            )

        return SingleResponse(
            request,
            data=UserAcquisitionSerializer.Base.model_validate(
                user_acquisition, context={"user": request.user, "request": request}
            ),
            status=HTTPStatus.CREATED,
        )


@openapi.parameter(
    name="user_acquisition_id", param_type=UUID, location=ParameterLocation.PATH, description="UserAcquisition UUID"
)
class UserAcquisitionDetail(SecuredView):
    @openapi.metadata(
        description="Retrieve detailed information about a specific user acquisition record. Returns acquisition metadata, associated entry details, user information, and access permissions. Validates that the requesting user has permission to view the acquisition record.",
        tags=["User Acquisitions"],
        summary="Get user acquisition details",
    )
    def get(self, request, user_acquisition_id: UUID):
        try:
            user_acquisition = UserAcquisition.objects.get(pk=user_acquisition_id)
        except UserAcquisition.DoesNotExist as e:
            raise ProblemDetailException(
                _("User acquisition not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        # The grant's own record stays visible to its holder after it expired (#215): `expire_at` tells why
        # the content no longer opens. Only reading the content checks expiry.
        if not has_object_permission("check_user_acquisition_view", request.user, user_acquisition):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return SingleResponse(
            request,
            data=UserAcquisitionSerializer.Base.model_validate(
                user_acquisition, context={"user": request.user, "request": request}
            ),
        )

    @openapi.metadata(
        description="Revoke a user acquisition: it expires now (IP-036). Every read path refuses it from the next "
        "request on (downloads answer 410, ranges of an open reading session included); the record, its annotations "
        "and its history stay. Allowed for its holder, a manager of the entry's catalog and superusers. Revoking an "
        "already expired grant changes nothing.",
        tags=["User Acquisitions"],
        summary="Revoke user acquisition",
    )
    @openapi.response(HTTPStatus.NO_CONTENT, "Revoked (or already expired)")
    @openapi.response(HTTPStatus.FORBIDDEN, "Not the holder or a manager of the catalog")
    @openapi.response(HTTPStatus.NOT_FOUND, "No such user acquisition")
    def delete(self, request, user_acquisition_id: UUID):
        user_acquisition = (
            UserAcquisition.objects.select_related("acquisition__entry__catalog")
            .filter(pk=user_acquisition_id)
            .first()
        )
        if user_acquisition is None:
            raise ProblemDetailException(
                _("User acquisition not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
            )

        holder = request.user.is_authenticated and user_acquisition.user_id == request.user.pk
        catalog = user_acquisition.acquisition.entry.catalog
        if not holder and not has_object_permission("check_catalog_manage", request.user, catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if not user_acquisition.is_expired:
            # A save, not an update: the signals revoke the fast path's pins and expire the content grant (#175).
            user_acquisition.expire_at = timezone.now()
            user_acquisition.save(update_fields=["expire_at", "updated_at"])
            logger.info(
                "user_acquisition.revoked id=%s by=%s holder=%s",
                user_acquisition.pk,
                request.user.pk,
                holder,
            )
        return HttpResponse(status=HTTPStatus.NO_CONTENT)
