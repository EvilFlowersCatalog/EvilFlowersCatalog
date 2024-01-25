import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Optional, List, Type, TypeVar

from django.conf import settings
from django.core.paginator import Paginator, EmptyPage
from django.http import HttpResponse, HttpResponseRedirect
from django.utils.translation import gettext as _
from pydantic import BaseModel, RootModel

from apps.core.errors import (
    ProblemDetailException,
    DetailType,
    ValidationError,
    ProblemDetail,
)

ResponseType = TypeVar("ResponseType")


class SingleResponseModel(BaseModel):
    response: ResponseType


class PaginationModel(BaseModel):
    page: int
    limit: Optional[int]
    pages: int
    total: int


class PaginationResponseModel(BaseModel):
    items: RootModel[ResponseType]
    metadata: PaginationModel


@dataclass
class Ordering:
    columns: List[str]

    @classmethod
    def create_from_request(cls, request, aliases: dict = None) -> "Ordering":
        columns = []
        aliases = aliases or {}

        for column in request.GET.get("order_by", "created_at").split(","):
            column_name = column[1:] if column.startswith("-") else column
            if column_name in aliases.keys():
                columns.append(
                    f"-{aliases[column_name]}"
                    if column.startswith("-")
                    else aliases[column_name]
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
    def __init__(self, request, data: Optional[ResponseType] = None, **kwargs):
        params = {}
        if data is not None:
            content_types = str(request.headers.get("accept", "application/json"))
            content_types = content_types.replace(" ", "").split(",")
            content_types = list(map(lambda r: r.split(";")[0], content_types))

            if any(x in ["*/*", "application/json"] for x in content_types):
                params["content_type"] = "application/json"
                params["content"] = data.model_dump_json(by_alias=True)
            else:
                params["content_type"] = "application/json"
                params["status"] = HTTPStatus.NOT_ACCEPTABLE
                params["content"] = json.dumps(
                    {
                        "message": _("Not Acceptable"),
                        "metadata": {
                            "available": [
                                "application/json",
                            ],
                            "asked": ", ".join(content_types),
                        },
                    }
                )

        kwargs.update(params)
        super().__init__(**kwargs)


class SingleResponse(GeneralResponse):
    def __init__(self, request, data=None, **kwargs):
        if data is None:
            kwargs["status"] = HTTPStatus.NO_CONTENT
        else:
            data = SingleResponseModel(response=data)
        super().__init__(request=request, data=data, **kwargs)


class ErrorResponse(GeneralResponse):
    def __init__(self, request, payload: ProblemDetail, **kwargs):
        kwargs.setdefault("status", HTTPStatus.INTERNAL_SERVER_ERROR)
        super().__init__(request=request, data=payload, **kwargs)


class ValidationResponse(GeneralResponse):
    def __init__(self, request, payload: ValidationError, **kwargs):
        kwargs.setdefault("content_type", "application/problem+json")
        kwargs.setdefault("status", HTTPStatus.UNPROCESSABLE_ENTITY)
        super().__init__(request, payload, **kwargs)


class PaginationResponse(GeneralResponse):
    def __init__(
        self,
        request,
        qs,
        serializer: Type[BaseModel],
        ordering: Ordering = None,
        **kwargs,
    ):
        kwargs.setdefault("content_type", "application/json")

        # Ordering
        ordering = ordering if ordering else Ordering.create_from_request(request)
        qs = qs.order_by(*ordering.columns)

        # Pagination
        paginate = request.GET.get("paginate", "true") == "true"
        if paginate:
            limit = int(
                request.GET.get("limit", settings.EVILFLOWERS_PAGINATION_DEFAULT_LIMIT)
            )
            page = int(request.GET.get("page", 1))

            paginator = Paginator(qs, limit)

            try:
                paginator.validate_number(page)
            except EmptyPage as e:
                raise ProblemDetailException(
                    title=_("Page not found"),
                    status=HTTPStatus.NOT_FOUND,
                    previous=e,
                    detail_type=DetailType.OUT_OF_RANGE,
                    detail=_("That page contains no results"),
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

        super().__init__(
            request,
            PaginationResponseModel(
                items=RootModel[List[serializer]].model_validate(
                    list(items), from_attributes=True, context={"user": request.user}
                ),
                metadata=PaginationModel(
                    page=page, limit=limit, pages=num_pages, total=total
                ),
            ),
            **kwargs,
        )


class SeeOtherResponse(HttpResponseRedirect):
    status_code = 303


__all__ = [
    "SingleResponse",
    "ErrorResponse",
    "PaginationResponse",
    "ValidationResponse",
    "SeeOtherResponse",
    "Ordering",
]
