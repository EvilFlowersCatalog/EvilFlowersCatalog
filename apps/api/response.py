import json
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from typing import Type, Union, Optional, Any

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.utils.translation import gettext as _
from porcupine.base import Serializer

from apps.api.encoders import ApiJSONEncoder
from apps.api.errors import ValidationException, ApiException


@dataclass
class Ordering:
    class Order(Enum):
        ASC = 'asc'
        DESC = 'desc'

    column: str
    order: Order = Order.ASC

    @classmethod
    def create_from_request(cls, request, aliases: dict = None) -> 'Ordering':
        column = request.GET.get('order_by', 'created_at')
        aliases = aliases or {}

        if column in aliases:
            column = aliases.get(column)

        result = Ordering(column, Ordering.Order(request.GET.get('order', 'asc')))
        return result

    def __str__(self):
        return self.column if self.order == self.Order.ASC else f"-{self.column}"

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
                params['content_type'] = 'application/json'
                params['content'] = json.dumps(data, cls=ApiJSONEncoder, serializer=serializer)
            else:
                params['content_type'] = 'application/json'
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
        data = {
            'error': payload
        }

        super().__init__(request=request, data=data, **kwargs)

    @staticmethod
    def create_from_exception(e: ApiException) -> 'ErrorResponse':
        return ErrorResponse(e.request, e.payload, status=e.status_code)


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
        self, request, qs, page: int, limit: Union[int, None] = None, ordering: Ordering = None, **kwargs
    ):
        kwargs.setdefault('content_type', 'application/json')

        # Ordering
        ordering = ordering if ordering else Ordering('created_at')
        qs = qs.order_by(str(ordering))

        if limit is None:
            data = {
                'items': qs,
                'metadata': {
                    'page': int(page),
                    'limit': None,
                    'pages': 1,
                    'total': qs.count()
                }
            }
        else:
            paginator = Paginator(qs, limit)

            data = {
                'items': paginator.get_page(page),
                'metadata': {
                    'page': int(page),
                    'limit': paginator.per_page,
                    'pages': paginator.num_pages,
                    'total': paginator.count
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
