from django.utils.translation import gettext as _

from apps import openapi
from apps.api.filters.user_activities import UserActivityFilter
from apps.api.response import Ordering, PaginationResponse
from apps.api.serializers.user_activities import UserActivitySerializer
from apps.core.errors import UnauthorizedException
from apps.core.models import UserActivity
from apps.core.views import SecuredView


class UserActivityManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve the authenticated user's personal activity history, most recent first. Covers "
        "downloads (`entry_downloaded`, `license_downloaded`), shelf changes (`shelf_added`, `shelf_removed`), "
        "shared links (`acquisition_shared`), the loan lifecycle (`loan_created`, `loan_renewed`, `loan_returned`, "
        "`loan_expired`, `loan_revoked`, `loan_cancelled`) and the reservation lifecycle (`reservation_created`, "
        "`reservation_available`, `reservation_claimed`, `reservation_expired`, `reservation_cancelled`). "
        "Repeating the same action on the same entry does not add a row: `count` grows and `last_occurred_at` "
        "moves forward, while `created_at` keeps the first occurrence. Rows are kept for "
        "`EVILFLOWERS_ACTIVITY_RETENTION_DAYS` (365 by default) after their last occurrence.",
        tags=["Activity"],
        summary="List user's activity history",
    )
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to access your history"))

        activities = (
            UserActivityFilter(request.GET, queryset=UserActivity.objects.all(), request=request)
            .qs.select_related("entry", "entry__language")
            .prefetch_related(
                "entry__entry_authors__author",
                "entry__categories",
                "entry__feeds",
                "entry__acquisitions",
            )
        )
        ordering = (
            Ordering.create_from_request(request) if "order_by" in request.GET else Ordering(["-last_occurred_at"])
        )

        return PaginationResponse(
            request,
            activities,
            ordering=ordering,
            serializer=UserActivitySerializer.Base,
            serializer_context={"request": request},
        )
