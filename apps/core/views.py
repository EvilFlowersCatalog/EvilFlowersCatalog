from typing import Dict, Union

from django.conf import settings
from django.contrib.auth import load_backend
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.utils.translation import gettext as _
from django.views import View

from apps.core.errors import UnauthorizedException


class SecuredView(View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._backends: Dict[str, BaseBackend] = {}
        for schema, backend in settings.SECURED_VIEW_AUTHENTICATION_SCHEMAS.items():
            self._backends[schema.lower()] = load_backend(backend)

    def _authenticate(self, request) -> Union[AnonymousUser, AbstractBaseUser]:
        auth_header = request.headers.get("Authorization", "")

        if not auth_header:
            return AnonymousUser()

        auth_header = str(auth_header).split(" ")

        if len(auth_header) != 2:
            raise UnauthorizedException(
                request, detail=_("Invalid or missing Authorization header")
            )

        if not auth_header[0] in settings.SECURED_VIEW_AUTHENTICATION_SCHEMAS.keys():
            raise UnauthorizedException(
                request, detail=_("Unsupported authentication schema")
            )

        auth_params = {auth_header[0].lower(): auth_header[1]}

        return self._backends[auth_header[0].lower()].authenticate(
            request, **auth_params
        )

    def dispatch(self, request, *args, **kwargs):
        request.user = self._authenticate(request)

        return super().dispatch(request, *args, **kwargs)
