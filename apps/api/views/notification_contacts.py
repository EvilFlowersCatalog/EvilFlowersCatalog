from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.forms.notification_contacts import NotificationContactForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.notification_contacts import NotificationContactSerializer
from apps.core.errors import (
    ProblemDetailException,
    UnauthorizedException,
    ValidationException,
)
from apps.core.views import SecuredView
from apps.notifications.models import NotificationContact
from apps.notifications.services import NotificationService


class NotificationContactManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of the authenticated user's notification contacts. "
        "Notifications (reservation availability, license expiry, etc.) are delivered to the "
        "primary contact of each type.",
        tags=["Notification Contacts"],
        summary="List user's notification contacts",
    )
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to manage notification contacts"))

        contacts = NotificationContact.objects.filter(user=request.user).order_by("type", "-is_primary", "value")

        return PaginationResponse(
            request, contacts, serializer=NotificationContactSerializer.Base, serializer_context={"request": request}
        )

    @openapi.metadata(
        description="Create or update a notification contact for the authenticated user. "
        'Body: `{"type": "email", "value": "user@example.com", "is_primary": true}`. '
        "Saving a primary contact demotes any previous primary of the same type — notifications "
        "are delivered to the primary contact.",
        tags=["Notification Contacts"],
        summary="Create or update notification contact",
    )
    def post(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to manage notification contacts"))

        form = NotificationContactForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        contact = NotificationService.set_contact(
            user=request.user,
            contact_type=form.cleaned_data["type"],
            value=form.cleaned_data["value"],
            is_primary=form.cleaned_data.get("is_primary", True),
        )

        return SingleResponse(
            request,
            data=NotificationContactSerializer.Base.model_validate(contact, context={"request": request}),
            status=HTTPStatus.CREATED,
        )


class NotificationContactDetail(SecuredView):
    @openapi.metadata(
        description="Delete one of the authenticated user's notification contacts. "
        "Returns 404 if the contact does not exist or belongs to another user.",
        tags=["Notification Contacts"],
        summary="Delete notification contact",
    )
    def delete(self, request, contact_id: UUID):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You need to be logged in to manage notification contacts"))

        try:
            contact = NotificationContact.objects.get(pk=contact_id, user=request.user)
        except NotificationContact.DoesNotExist as e:
            raise ProblemDetailException(title=_("Not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        contact.delete()

        return SingleResponse(request)
