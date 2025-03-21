from apps.core.errors import ProblemDetailException, ValidationException
from apps.api.response import ErrorResponse, ValidationResponse


class ExceptionMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    @staticmethod
    def process_exception(request, exception):
        if isinstance(exception, ValidationException):
            return ValidationResponse(request, exception.payload, status=exception.status)
        elif isinstance(exception, ProblemDetailException):
            # FIXME: check this out, extra_headers propagation kinda sucks
            return ErrorResponse(
                request,
                exception.payload,
                status=exception.status,
                headers=exception.extra_headers,
            )


__all__ = ["ExceptionMiddleware"]
