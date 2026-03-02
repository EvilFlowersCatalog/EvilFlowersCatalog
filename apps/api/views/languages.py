from apps import openapi
from apps.api.serializers.entries import LanguageSerializer
from apps.api.response import PaginationResponse
from apps.core.models import Language
from apps.core.views import SecuredView


class LanguageManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of available languages that can be assigned to entries. This includes ISO language codes, native names, and English translations for internationalization support.",
        tags=["Enums"],
        summary="List available languages",
    )
    def get(self, request):
        return PaginationResponse(request, Language.objects.all(), serializer=LanguageSerializer.Base)
