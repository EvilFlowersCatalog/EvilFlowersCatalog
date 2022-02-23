import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Type, Optional, Any, List

from django.conf import settings
from django.core.paginator import Paginator, EmptyPage
from django.http import HttpResponse
from django.utils.translation import gettext as _
from porcupine.base import Serializer

from apps.api.encoders import ApiJSONEncoder
from apps.core.errors import ValidationException, ProblemDetailException


@dataclass
class Ordering:
    columns: List[str]

    @classmethod
    def create_from_request(cls, request, aliases: dict = None) -> 'Ordering':
        columns = []
        aliases = aliases or {}

        for column in request.GET.get('order_by', 'created_at').split(','):
            column_name = column[1:] if column.startswith("-") else column
            if column_name in aliases.keys():
                columns.append(
                    f"-{aliases[column_name]}" if column.startswith("-") else aliases[column_name]
                )
            else:
                columns.append(column)

        result = Ordering(columns)
        return result

    def __str__(self):
        return ",".join(self.columns)

    def __repr__(self):
        return self.__str__()


class GeneralResponse(HttpResponse):
    def __init__(
        self, request, data: Optional[Any] = None,
        serializer: Type[Serializer] = None, **kwargs
    ):
        params = {}
        if data is not None:
            content_type = request.headers.get('accept', 'application/json')
            if content_type in ['*/*', 'application/json']:
                params['content'] = json.dumps(data, cls=ApiJSONEncoder, serializer=serializer)
            else:
                params['status'] = HTTPStatus.NOT_ACCEPTABLE
                params['content'] = json.dumps({
                    'message': _("Not Acceptable"),
                    'metadata': {
                        'available': [
                            'application/json',
                        ],
                        'asked': content_type
                    }
                })

        kwargs.update(params)
        super().__init__(**kwargs)


class SingleResponse(GeneralResponse):
    def __init__(self, request, data=None, **kwargs):
        if data is None:
            kwargs['status'] = HTTPStatus.NO_CONTENT
        else:
            data = {
                'response': data,
            }
        super().__init__(request=request, data=data, **kwargs)


class ErrorResponse(GeneralResponse):
    def __init__(self, request, payload: dict, **kwargs):
        super().__init__(request=request, data=payload, **kwargs)

    @staticmethod
    def create_from_exception(e: ProblemDetailException) -> 'ErrorResponse':
        return ErrorResponse(e.request, e.payload, status=e.status, headers=e.extra_headers)


class ValidationResponse(GeneralResponse):
    def __init__(self, request, payload: dict, **kwargs):
        kwargs.setdefault("content_type", "application/problem+json")
        data = {
            "type": "/validation-error",
            "title": "Invalid request parameters",
            "status": HTTPStatus.UNPROCESSABLE_ENTITY,
            'errors': payload,
        }

        super().__init__(request, data, status=HTTPStatus.UNPROCESSABLE_ENTITY, **kwargs)

    @staticmethod
    def create_from_exception(e: ValidationException) -> 'ValidationResponse':
        return ValidationResponse(e.request, e.payload, status=HTTPStatus.UNPROCESSABLE_ENTITY)


class PaginationResponse(GeneralResponse):
    def __init__(
        self, request, qs, ordering: Ordering = None, **kwargs
    ):
        kwargs.setdefault('content_type', 'application/json')

        # Ordering
        ordering = ordering if ordering else Ordering.create_from_request(request)
        qs = qs.order_by(*ordering.columns)

        # Pagination
        paginate = request.GET.get('paginate', 'true') == 'true'
        if paginate:
            limit = int(request.GET.get('limit', settings.PAGINATION['DEFAULT_LIMIT']))
            page = int(request.GET.get('page', 1))

            paginator = Paginator(qs, limit)

            try:
                paginator.validate_number(page)
            except EmptyPage as e:
                raise ProblemDetailException(
                    request,
                    title=_('Page not found'),
                    status=HTTPStatus.NOT_FOUND,
                    previous=e,
                    detail_type='out_of_range',
                    detail=_('That page contains no results')
                )

            items = paginator.get_page(page)
            num_pages = paginator.num_pages
            total = paginator.count
        else:
            limit = None
            page = 1
            items = qs
            num_pages = 1
            total = qs.count()

        data = {
            'items': items,
            'metadata': {
                'page': page,
                'limit': limit,
                'pages': num_pages,
                'total': total
            }
        }

        super().__init__(request, data, **kwargs)


__all__ = [
    "SingleResponse",
    "ErrorResponse",
    "PaginationResponse",
    "ValidationResponse",
    "Ordering"
]
