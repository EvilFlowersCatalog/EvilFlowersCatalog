import traceback
import warnings
from enum import Enum
from http import HTTPStatus
from typing import Tuple, Optional, List, Any

from django.conf import settings
from django.utils.text import slugify
from django_api_forms.forms import Form

from django.utils.translation import gettext as _

from apps.api.serializers import Serializer


class DetailType(Enum):
    OUT_OF_RANGE = "/out-of-range"
    NOT_FOUND = "/not-found"
    VALIDATION_ERROR = "/validation-error"
    CONFLICT = "/conflict"


class ProblemDetail(Serializer):
    title: str
    type: Optional[DetailType] = None
    detail: Optional[str] = None
    trace: Optional[List[str]] = None
    additional_data: Optional[Any] = None


class ValidationErrorItem(Serializer):
    code: Optional[str] = None
    message: str
    path: Optional[List[str]] = None


class ValidationError(ProblemDetail):
    validation_errors: List[ValidationErrorItem]


class ProblemDetailException(Exception):
    def __init__(
        self,
        title: str,
        *,
        status: int = HTTPStatus.INTERNAL_SERVER_ERROR,
        previous: Optional[BaseException] = None,
        to_sentry: Optional[bool] = False,
        additional_data: Optional[dict] = None,
        detail_type: Optional[DetailType] = None,
        detail: Optional[str] = None,
        extra_headers: Optional[Tuple[Tuple]] = (),
    ):
        super().__init__(title)

        self._status_code = status
        self._title = title
        self._type = detail_type
        self._detail = detail
        self._previous = previous
        self._extra_headers = extra_headers

        if additional_data:
            self._additional_data = additional_data
        else:
            self._additional_data = {}

        if to_sentry:
            try:
                import sentry_sdk

                with sentry_sdk.push_scope() as scope:
                    for key, value in self.__dict__.items():
                        scope.set_extra(key, value)
                    sentry_sdk.capture_exception(self)
            except ImportError:
                warnings.warn("sentry_sdk module is not installed")

    @property
    def status(self) -> int:
        return self._status_code

    @property
    def title(self) -> str:
        return self._title

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def type(self) -> DetailType:
        return self._type

    @property
    def previous(self) -> BaseException:
        return self._previous

    @property
    def extra_headers(self) -> Tuple[Tuple]:
        return self._extra_headers

    @property
    def payload(self) -> ProblemDetail:
        return ProblemDetail(
            title=self.title,
            type=self.type,
            detail=self.detail,
            trace=traceback.format_exc().split("\n") if settings.DEBUG else None,
            additional_data=self._additional_data,
        )


class AuthorizationException(ProblemDetailException):
    def __init__(self, request, detail: Optional[str] = None, previous: Optional[BaseException] = None):
        if request.user.is_authenticated:
            super().__init__(
                _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN, detail=detail, previous=previous
            )
        else:
            super().__init__(
                _("Unauthorized"),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    (
                        "WWW-Authenticate",
                        f"Basic realm={slugify(settings.INSTANCE_NAME)},"
                        f' Bearer realm="{slugify(settings.INSTANCE_NAME)}"',
                    ),
                ),
                detail=detail,
                previous=previous,
            )


class UnauthorizedException(ProblemDetailException):
    def __init__(self, detail: Optional[str] = None, previous: Optional[BaseException] = None):
        super().__init__(
            _("Unauthorized"),
            status=HTTPStatus.UNAUTHORIZED,
            extra_headers=(
                (
                    "WWW-Authenticate",
                    f"Basic realm={slugify(settings.INSTANCE_NAME)},"
                    f' Bearer realm="{slugify(settings.INSTANCE_NAME)}"',
                ),
            ),
            detail=detail,
            previous=previous,
        )


class ValidationException(ProblemDetailException):
    def __init__(self, form: Form):
        super().__init__(_("Validation error!"), status=HTTPStatus.UNPROCESSABLE_ENTITY)
        self._form = form

    @property
    def payload(self) -> ValidationError:
        return ValidationError(
            title=_("Invalid request parameters"),
            type=DetailType.VALIDATION_ERROR,
            validation_errors=[
                ValidationErrorItem(
                    code=item.code,
                    message=item.message % (item.params or ()),
                    path=getattr(item, "path", ["$body"]),
                )
                for item in self._form.errors
            ],
        )
