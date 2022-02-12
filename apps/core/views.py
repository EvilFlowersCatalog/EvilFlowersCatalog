from http import HTTPStatus
from typing import Dict

from django.conf import settings
from django.contrib.auth import load_backend
from django.contrib.auth.backends import BaseBackend
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views import View

from apps.core.errors import ProblemDetailException, UnauthorizedException


class SecuredView(View):
    UNSECURED_METHODS = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._backends: Dict[str, BaseBackend] = {}
        for schema, backend in settings.SECURED_VIEW_AUTHENTICATION_SCHEMAS.items():
            self._backends[schema.lower()] = load_backend(backend)

    def _authenticate(self, request):
        auth_header = request.headers.get('Authorization', '').split(' ')
        if len(auth_header) != 2:
            raise ProblemDetailException(
                request, _("Invalid or missing Authorization header"), status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    ('WWW-Authenticate', f'Bearer realm="{slugify(settings.INSTANCE_NAME)}"'),
                )
            )

        if not auth_header[0] in settings.SECURED_VIEW_AUTHENTICATION_SCHEMAS.keys():
            raise UnauthorizedException(request)

        auth_params = {
            auth_header[0].lower(): auth_header[1]
        }
        request.user = self._backends[auth_header[0].lower()].authenticate(request, **auth_params)

    def dispatch(self, request, *args, **kwargs):
        if request.method not in self.UNSECURED_METHODS:
            self._authenticate(request)

        return super().dispatch(request, *args, **kwargs)
