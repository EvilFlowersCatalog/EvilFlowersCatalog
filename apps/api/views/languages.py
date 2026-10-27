from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.filters.languages import LanguageFilter
from apps.api.forms.languages import LanguageForm
from apps.api.serializers.entries import LanguageSerializer
from apps.api.response import PaginationResponse, SingleResponse
from apps.core.errors import ProblemDetailException, ValidationException
from apps.core.models import Language
from apps.core.views import SecuredView


class LanguageManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of available languages that can be assigned to entries. This includes ISO language codes, native names, and English translations for internationalization support. Use `translatable=true` to list languages in which feed titles and category labels can be entered.",
        tags=["Enums"],
        summary="List available languages",
    )
    def get(self, request):
        languages = LanguageFilter(request.GET, queryset=Language.objects.all(), request=request).qs

        return PaginationResponse(request, languages, serializer=LanguageSerializer.Base)


class LanguageDetail(SecuredView):
    @openapi.metadata(
        description="Enable or disable a language for translating feed titles and category labels. Disabling a language keeps already stored translations. Requires superuser permissions.",
        tags=["Enums"],
        summary="Update language settings",
    )
    def put(self, request, language_id: UUID):
        if not request.user.is_superuser:
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        form = LanguageForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        try:
            language = Language.objects.get(pk=language_id)
        except Language.DoesNotExist as e:
            raise ProblemDetailException(_("Language not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        # Every localized text must have the default language translation, so it cannot be turned off
        if language.alpha2 == settings.EVILFLOWERS_DEFAULT_LANGUAGE and not form.cleaned_data["translatable"]:
            raise ProblemDetailException(
                _("The default language cannot be disabled"), status=HTTPStatus.UNPROCESSABLE_ENTITY
            )

        form.populate(language)
        language.save()

        return SingleResponse(request, data=LanguageSerializer.Base.model_validate(language))
