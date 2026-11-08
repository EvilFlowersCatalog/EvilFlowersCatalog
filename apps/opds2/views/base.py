from http import HTTPStatus

from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views import View
from object_checker.base_object_checker import has_object_permission

from apps.core.errors import ProblemDetailException, UnauthorizedException
from apps.core.models import Catalog
from apps.core.views import SecuredView
from apps.opds2.views.auth import authentication_required

OPDS_JSON_CONTENT_TYPE = "application/opds+json"
OPDS_PUBLICATION_CONTENT_TYPE = "application/opds-publication+json"


class Opds2CatalogView(SecuredView):
    """Base view for all OPDS 2.0 catalog endpoints.

    Handles catalog resolution, authentication, and access control.
    """

    def __init__(self, *args, **kwargs):
        self.catalog = None
        super().__init__(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        try:
            return self._dispatch(request, *args, **kwargs)
        except ProblemDetailException as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                return authentication_required(request, self.catalog)
            raise

    def _dispatch(self, request, *args, **kwargs):
        try:
            self.catalog = Catalog.objects.get(url_name=kwargs["catalog_name"])
        except Catalog.DoesNotExist:
            raise ProblemDetailException(_("Catalog not found"), status=HTTPStatus.NOT_FOUND)
        except KeyError as e:
            raise ProblemDetailException(_("Internal server error"), status=HTTPStatus.NOT_FOUND, previous=e)

        request.user = self._authenticate(request)

        if not self.catalog.is_public and not request.user.is_authenticated:
            raise UnauthorizedException()

        if not has_object_permission("check_catalog_read", request.user, self.catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return View.dispatch(self, request, *args, **kwargs)

    @staticmethod
    def opds_response(data: dict, status: int = 200, content_type: str = OPDS_JSON_CONTENT_TYPE) -> JsonResponse:
        return JsonResponse(data, content_type=content_type, status=status)

    @staticmethod
    def publication_response(data: dict, status: int = 200) -> JsonResponse:
        """A single OPDS 2.0 publication (not a feed): readers pick the parser by type."""
        return JsonResponse(data, content_type=OPDS_PUBLICATION_CONTENT_TYPE, status=status)
