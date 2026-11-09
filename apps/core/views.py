from http import HTTPStatus
from typing import Dict, Union

from django.conf import settings
from django.contrib.auth import load_backend
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.utils.translation import gettext as _
from django.views import View

from apps.core import ratelimit
from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException


class SecuredView(View):
    #: Counted against the client's request budget (#21). Set False on a view that must never be refused for
    #: volume, e.g. a server-to-server callback.
    rate_limited = True

    #: Views a user whose password must be changed may still call (#8): reading their own account and changing
    #: the password. Everything else answers 403 with `/password-change-required` until they do.
    allowed_while_password_change_required = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._backends: Dict[str, BaseBackend] = {}
        for schema, backend in settings.SECURED_VIEW_AUTHENTICATION_SCHEMAS.items():
            self._backends[schema.lower()] = load_backend(backend)

    def _authenticate(self, request) -> Union[AnonymousUser, AbstractBaseUser]:
        auth_header = request.headers.get("Authorization", "")

        if not auth_header:
            token = request.GET.get("access_token", None)

            if not token:
                return AnonymousUser()

            auth_header = f"Bearer {token}"

        auth_header = str(auth_header).split(" ")

        if len(auth_header) != 2:
            raise UnauthorizedException(detail=_("Invalid or missing Authorization header"))

        if not auth_header[0] in settings.SECURED_VIEW_AUTHENTICATION_SCHEMAS.keys():
            raise UnauthorizedException(detail=_("Unsupported authentication schema"))

        auth_params = {auth_header[0].lower(): auth_header[1]}

        return self._backends[auth_header[0].lower()].authenticate(request, **auth_params)

    def dispatch(self, request, *args, **kwargs):
        try:
            request.user = self._authenticate(request)
        except ProblemDetailException as error:
            # Refused credentials count against the IP: passwords cannot be tried freely (#21).
            if error.status == HTTPStatus.UNAUTHORIZED:
                decision = ratelimit.check_failed_authentication(request)
                if decision is not None and not decision.allowed:
                    raise self._rate_limited(decision)
            raise

        decision = ratelimit.check(request, self)
        if decision is not None and not decision.allowed:
            raise self._rate_limited(decision)

        if (
            getattr(request.user, "password_change_required", False)
            and not self.allowed_while_password_change_required
        ):
            raise ProblemDetailException(
                _("You have to change your password first."),
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.PASSWORD_CHANGE_REQUIRED,
            )

        response = super().dispatch(request, *args, **kwargs)
        if decision is not None:
            for name, value in decision.headers():
                response[name] = value
        return response

    @staticmethod
    def _rate_limited(decision) -> ProblemDetailException:
        return ProblemDetailException(
            _("Too many requests"),
            status=HTTPStatus.TOO_MANY_REQUESTS,
            detail_type=DetailType.RATE_LIMITED,
            detail=_("The request limit for this period is used up; see Retry-After."),
            extra_headers=decision.headers(),
        )
