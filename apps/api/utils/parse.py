from http import HTTPStatus
from typing import Optional

from django.utils.translation import gettext as _

from apps.core.errors import DetailType, ProblemDetailException


def parse_int_query(
    request,
    name: str,
    default: Optional[int] = None,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> Optional[int]:
    """Parse an integer query-string parameter, raising 400 on malformed input.

    - Returns ``default`` when the parameter is absent.
    - Raises ``ProblemDetailException`` with status 400 when the parameter
      cannot be parsed as int, or falls outside ``[min_value, max_value]``.
    """
    raw = request.GET.get(name)
    if raw is None or raw == "":
        return default

    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProblemDetailException(
            _("Invalid value for query parameter '%s'") % (name,),
            status=HTTPStatus.BAD_REQUEST,
            detail_type=DetailType.VALIDATION_ERROR,
            detail=_("'%s' must be an integer") % (name,),
            previous=exc,
        )

    if min_value is not None and value < min_value:
        raise ProblemDetailException(
            _("Invalid value for query parameter '%s'") % (name,),
            status=HTTPStatus.BAD_REQUEST,
            detail_type=DetailType.OUT_OF_RANGE,
            detail=_("'%s' must be >= %s") % (name, min_value),
        )

    if max_value is not None and value > max_value:
        raise ProblemDetailException(
            _("Invalid value for query parameter '%s'") % (name,),
            status=HTTPStatus.BAD_REQUEST,
            detail_type=DetailType.OUT_OF_RANGE,
            detail=_("'%s' must be <= %s") % (name, max_value),
        )

    return value
