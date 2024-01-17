from http import HTTPStatus

from django.utils.translation import gettext as _
from django.views import View
from object_checker.base_object_checker import has_object_permission

from apps.core.errors import ProblemDetailException, UnauthorizedException
from apps.core.models import Catalog
from apps.core.views import SecuredView


class OpdsView(SecuredView):
    def __init__(self, *args, **kwargs):
        self.catalog = None
        super().__init__(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        try:
            self.catalog = Catalog.objects.get(url_name=kwargs["catalog_name"])
        except Catalog.DoesNotExist:
            raise ProblemDetailException(
                request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND
            )
        except KeyError as e:
            raise ProblemDetailException(
                request,
                _("Internal server error"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
            )

        request.user = self._authenticate(request)

        if not self.catalog.is_public and not request.user.is_authenticated:
            raise UnauthorizedException(request)

        if not has_object_permission("check_catalog_read", request.user, self.catalog):
            raise ProblemDetailException(
                request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN
            )

        return View.dispatch(self, request, *args, **kwargs)
