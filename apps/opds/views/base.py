from http import HTTPStatus

from django.utils.translation import gettext as _
from django.views import View

from apps.core.errors import ProblemDetailException
from apps.core.models import Catalog
from apps.core.views import SecuredView


class OpdsView(SecuredView):
    def __init__(self, *args, **kwargs):
        self.catalog = None
        super().__init__(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        try:
            self.catalog = Catalog.objects.get(url_name=kwargs['catalog_name'])
        except Catalog.DoesNotExist:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND)
        except KeyError as e:
            raise ProblemDetailException(request, _("Internal server error"), status=HTTPStatus.NOT_FOUND, previous=e)

        if request.method not in self.UNSECURED_METHODS and not self.catalog.is_public:
            self._authenticate(request)

        return View.dispatch(self, request, *args, **kwargs)
