from http import HTTPStatus

from django.contrib.auth import authenticate
from django.utils.translation import gettext as _
from django.views import View

from apps.api.errors import ProblemDetailException


class SecuredView(View):
    UNSECURED_METHODS = []

    def _authenticate(self, request):
        auth_header = request.headers.get('Authorization', '').split(' ')
        if len(auth_header) != 2:
            raise ProblemDetailException(
                request, _("Invalid or missing Authorization header"), status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    ('WWW-Authenticate', 'Bearer realm="evil-flowers-catalog'),  # TODO: application name
                )
            )

        if auth_header[0] in ('Bearer', 'Basic'):
            auth_params = {
                auth_header[0].lower(): auth_header[1]
            }
        else:
            raise ProblemDetailException(
                request,
                _("Unauthorized"),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    ('WWW-Authenticate', 'Bearer realm="evil-flowers-catalog'),  # TODO: application name
                )
            )

        request.user = authenticate(request, **auth_params)

    def dispatch(self, request, *args, **kwargs):
        if request.method not in self.UNSECURED_METHODS:
            self._authenticate(request)

        return super().dispatch(request, *args, **kwargs)
