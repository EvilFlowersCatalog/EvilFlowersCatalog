from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.api_keys import ApiKeyFilter
from apps.api.forms.api_keys import ApiKeyForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.api_keys import ApiKeySerializer
from apps.core.models import ApiKey
from apps.core.views import SecuredView


class ApiKeyManagement(SecuredView):
    @openapi.metadata(
        description="Generate a new API key for the authenticated user. Creates a unique API key that can be used for programmatic access to the API. Superusers can create keys for other users by specifying a user_id. Returns the generated key details including the key value for initial setup.",
        tags=["API Keys"],
        summary="Create new API key"
    )
    def post(self, request):
        form = ApiKeyForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if "user_id" in form.cleaned_data.keys() and not request.user.is_superuser:
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        api_key = ApiKey(user=request.user)
        form.populate(api_key)
        api_key.save()

        return SingleResponse(
            request,
            data=ApiKeySerializer.Base.model_validate(api_key),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(
        description="Retrieve a paginated list of API keys in the system. Returns API key metadata including creation date, user association, and key status. Supports filtering and pagination. Superusers can view all keys, while regular users see only their own keys.",
        tags=["API Keys"],
        summary="List API keys"
    )
    def get(self, request):
        api_keys = ApiKeyFilter(request.GET, queryset=ApiKey.objects.all(), request=request).qs

        return PaginationResponse(request, api_keys, serializer=ApiKeySerializer.Base)


class ApiKeyDetail(SecuredView):
    @openapi.metadata(
        description="Permanently delete an API key, immediately revoking its access to the API. Only the key owner or superusers can delete keys. Once deleted, the key cannot be recovered and any applications using it will lose access. Returns 404 if key doesn't exist or user lacks permission.",
        tags=["API Keys"],
        summary="Delete API key"
    )
    def delete(self, request, api_key_id: UUID):
        try:
            api_key = ApiKey.objects.get(pk=api_key_id)
        except ApiKey.DoesNotExist as e:
            raise ProblemDetailException(_("ApiKey not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not request.user.is_superuser and api_key.user_id != request.user.id:
            raise ProblemDetailException(_("ApiKey not found"), status=HTTPStatus.NOT_FOUND)

        api_key.delete()

        return SingleResponse(request)
