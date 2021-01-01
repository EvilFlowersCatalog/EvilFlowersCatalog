from apps.api.errors import ApiException, ValidationException
from apps.api.response import ErrorResponse, ValidationResponse


class ExceptionMiddleware(object):

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    @staticmethod
    def process_exception(request, exception):
        if isinstance(exception, ApiException):
            return ErrorResponse.create_from_exception(exception)
        if isinstance(exception, ValidationException):
            return ValidationResponse.create_from_exception(exception)


__all__ = [
    'ExceptionMiddleware'
]
