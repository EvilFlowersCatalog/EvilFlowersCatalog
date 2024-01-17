from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.api_keys import ApiKeyFilter
from apps.api.forms.api_keys import ApiKeyForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.api_keys import ApiKeySerializer
from apps.core.models import ApiKey
from apps.core.views import SecuredView


class ApiKeyManagement(SecuredView):
    def post(self, request):
        form = ApiKeyForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if "user_id" in form.cleaned_data.keys() and not request.user.is_superuser:
            raise ProblemDetailException(
                request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN
            )

        api_key = ApiKey(user=request.user)
        form.populate(api_key)
        api_key.save()

        return SingleResponse(
            request,
            data=ApiKeySerializer.Base.model_validate(api_key),
            status=HTTPStatus.CREATED,
        )

    def get(self, request):
        api_keys = ApiKeyFilter(
            request.GET, queryset=ApiKey.objects.all(), request=request
        ).qs

        return PaginationResponse(request, api_keys, serializer=ApiKeySerializer.Base)


class ApiKeyDetail(SecuredView):
    def delete(self, request, api_key_id: UUID):
        try:
            api_key = ApiKey.objects.get(pk=api_key_id)
        except ApiKey.DoesNotExist as e:
            raise ProblemDetailException(
                request, _("ApiKey not found"), status=HTTPStatus.NOT_FOUND, previous=e
            )

        if not request.user.is_superuser and api_key.user_id != request.user.id:
            raise ProblemDetailException(
                request, _("ApiKey not found"), status=HTTPStatus.NOT_FOUND
            )

        api_key.delete()

        return SingleResponse(request, status=HTTPStatus.NO_CONTENT)
