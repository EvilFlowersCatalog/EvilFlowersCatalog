from datetime import datetime, timedelta
from http import HTTPStatus
from uuid import UUID

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.dateparse import parse_date

from apps import openapi
from apps.api.response import SingleResponse, PaginationResponse
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.readium.models import License
from apps.readium.services import LicenseAvailabilityService
from apps.readium.serializers import LicenseSerializer


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
        start_date_str = request.GET.get('start_date')
        end_date_str = request.GET.get('end_date')
        
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
            entry=entry,
            start_date=start_date,
            end_date=end_date
        )
        
        return SingleResponse(request, data=availability)


class EntryLicensesView(SecuredView):
    @openapi.metadata(
        description="Get all licenses for an entry. Returns licenses that the authenticated user can view based on permissions.",
        tags=["Readium"],
        summary="Get entry licenses",
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
        
        # Filter licenses based on user permissions
        licenses = License.objects.filter(entry=entry)
        
        # Users can only see their own licenses unless they're admin
        if not request.user.is_staff:
            licenses = licenses.filter(user=request.user)
        
        return PaginationResponse(request, licenses, serializer=LicenseSerializer.Base)
    
    @openapi.metadata(
        description="Create a new license for the authenticated user to access an entry. Validates availability and user permissions before creating the license.",
        tags=["Readium"],
        summary="Create entry license",
    )
    def post(self, request, entry_id: UUID):
        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist as e:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )
        
        # Parse request data
        start_date_str = request.data.get('start_date')
        duration_days = request.data.get('duration_days', 14)
        passphrase_hint = request.data.get('passphrase_hint')
        
        start_date = None
        if start_date_str:
            start_date = parse_date(start_date_str)
            if start_date:
                start_date = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
        
        try:
            license = LicenseAvailabilityService.create_license_for_user(
                entry=entry,
                user=request.user,
                start_date=start_date,
                duration_days=duration_days,
                passphrase_hint=passphrase_hint
            )
            
            return SingleResponse(
                request,
                data=LicenseSerializer.Base.model_validate(license),
                status=HTTPStatus.CREATED
            )
            
        except ValueError as e:
            raise ProblemDetailException(
                str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR
            )