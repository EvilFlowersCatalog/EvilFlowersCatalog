from datetime import datetime
from http import HTTPStatus
from uuid import UUID

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.dateparse import parse_date

from apps import openapi
from apps.api.response import SingleResponse
from apps.core.errors import ProblemDetailException, DetailType
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.readium.services import LicenseAvailabilityService


class EntryAvailabilityView(SecuredView):
    @openapi.metadata(
        description="Get availability calendar for a readium-enabled entry. Returns information about available borrowing slots over a specified date range, suitable for displaying a calendar interface.",
        tags=["Readium"],
        summary="Get entry availability calendar",
    )
    def get(self, request, entry_id: UUID):
        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist as e:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        # Parse date parameters
        start_date_str = request.GET.get("start_date")
        end_date_str = request.GET.get("end_date")

        start_date = None
        end_date = None

        if start_date_str:
            start_date = parse_date(start_date_str)
            if start_date:
                start_date = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))

        if end_date_str:
            end_date = parse_date(end_date_str)
            if end_date:
                end_date = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))

        # Get availability data
        availability = LicenseAvailabilityService.get_entry_availability(
            entry=entry, start_date=start_date, end_date=end_date
        )

        return SingleResponse(request, data=availability)
