from apps import openapi
from apps.api.serializers.entries import LanguageSerializer
from apps.api.response import PaginationResponse
from apps.core.models import Language
from apps.core.views import SecuredView


class LanguageManagement(SecuredView):
    @openapi.metadata(description="List languages", tags=["Enums"])
    def get(self, request):
        return PaginationResponse(request, Language.objects.all(), serializer=LanguageSerializer.Base)

