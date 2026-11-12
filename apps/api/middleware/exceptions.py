try:
    from opentelemetry import trace
except ImportError:
    trace = None

from apps.core.errors import ProblemDetailException, ValidationException
from apps.api.response import ErrorResponse, ValidationResponse


class ExceptionMiddleware(object):
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    @staticmethod
    def process_exception(request, exception):
        # Record the exception on the current server span before converting it to a
        # response. This is the only place handled exceptions reach the span: for
        # the ones converted below, Django never calls the OTel middleware's
        # `process_exception`.
        #
        # Status ERROR only for a server fault (5xx, or an exception not converted
        # here). HTTP semantic conventions leave 4xx server spans unset; marking a
        # 404 or an expired token as an error would inflate Tempo's span-metric
        # error rate (IP-019 Q4).
        if trace is not None:
            span = trace.get_current_span()
            if span.is_recording():
                span.record_exception(exception)
                status = getattr(exception, "status", None) if isinstance(exception, ProblemDetailException) else None
                if status is None or status >= 500:
                    span.set_status(trace.StatusCode.ERROR, str(exception))

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
