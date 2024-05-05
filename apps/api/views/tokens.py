import base64
from http import HTTPStatus

from authlib.jose import JoseError
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext as _
from django.views import View

from apps.api.forms.tokens import AccessTokenForm, RefreshTokenForm
from apps.api.serializers.tokens import TokenSerializer
from apps.core.auth import JWTFactory, BasicBackend
from apps.core.errors import (
    ValidationException,
    UnauthorizedException,
)
from apps.api.response import SingleResponse
from apps.core.views import SecuredView


class AccessTokenManagement(SecuredView):
    def post(self, request):
        form = AccessTokenForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        backend = BasicBackend()
        user = backend.authenticate(
            request,
            basic=base64.b64encode(
                f"{form.cleaned_data['username']}:{form.cleaned_data['password']}".encode()
            ).decode(),
        )

        if not user:
            raise UnauthorizedException()

        access_token = JWTFactory(user.pk).access()
        jti, refresh_token = JWTFactory(user.pk).refresh()

        cache.set(f"refresh_token:{jti}", jti, settings.SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION.seconds)

        return SingleResponse(
            request,
            TokenSerializer.Access(access_token=access_token, refresh_token=refresh_token, user=user),
        )


class RefreshTokenManagement(View):
    def post(self, request):
        form = RefreshTokenForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        try:
            claims = JWTFactory.decode(form.cleaned_data["refresh"])
        except JoseError as e:
            raise UnauthorizedException(_("Invalid token."), previous=e)

        print(cache.get(f"refresh_token:{claims['jti']}"))
        print(claims)
        print(claims['jti'])

        if not cache.get(f"refresh_token:{claims['jti']}"):
            raise UnauthorizedException()

        return SingleResponse(
            request,
            TokenSerializer.Refresh(access_token=JWTFactory(claims["sub"]).access()),
            status=HTTPStatus.OK,
        )
