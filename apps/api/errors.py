import traceback
from http import HTTPStatus

import sentry_sdk
from django.conf import settings
from django_api_forms.forms import Form

from django.utils.translation import gettext as _


class ApiException(Exception):
    def __init__(
        self,
        request,
        message: str,
        status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR,
        previous: Exception = None,
        to_sentry: bool = False,
        additional_data: dict = None
    ):
        super().__init__(message)

        self._request = request
        self._status_code = status_code
        self._message = message
        self._previous = previous

        if additional_data:
            self._additional_data = additional_data
        else:
            self._additional_data = {}

        if to_sentry:
            with sentry_sdk.push_scope() as scope:
                for key, value in self.__dict__.items():
                    scope.set_extra(key, value)
                sentry_sdk.capture_exception(self)

    @property
    def request(self):
        return self._request

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def message(self) -> str:
        return self._message

    @property
    def previous(self) -> Exception:
        return self._previous

    @property
    def payload(self) -> dict:
        result = {
            'message': self.message
        }

        if settings.DEBUG:
            result['trace'] = traceback.format_exc().split("\n")

        return result


class ValidationException(ApiException):
    def __init__(self, request, form: Form):
        super().__init__(request, _("Validation error!"), status_code=HTTPStatus.UNPROCESSABLE_ENTITY)
        self._form = form

    @property
    def payload(self) -> dict:
        return self._form.errors
