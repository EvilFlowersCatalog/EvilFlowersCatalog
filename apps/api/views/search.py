from http import HTTPStatus
from uuid import UUID

from apps import openapi
from apps.api.response import SingleResponse
from apps.api.serializers.search import EntrySearchResponseSerializer
from apps.api.services.entry_search_service import EntrySearchService
from apps.api.services.search_service_client import SearchServiceClientError
from apps.api.views.entries import shelf_record_mapping
from apps.core.errors import ProblemDetailException
from apps.core.views import SecuredView


class EntrySearchView(SecuredView):
    @openapi.metadata(
        description="Search catalog entries through the external search service and map ranked hits back to "
        "catalog entries the current user is allowed to access.",
        tags=["Entries"],
        summary="Search entries through external search service",
    )
    def get(self, request):
        query = (request.GET.get("query") or "").strip()
        if not query:
            raise ProblemDetailException("Missing query", status=HTTPStatus.BAD_REQUEST, detail="Query is required")

        mode = request.GET.get("mode", "elasticsearch")
        if mode not in {"elasticsearch", "semantic"}:
            raise ProblemDetailException(
                "Invalid search mode",
                status=HTTPStatus.BAD_REQUEST,
                detail="Mode must be either 'elasticsearch' or 'semantic'",
            )

        try:
            top_k = int(request.GET.get("top_k", 10))
        except ValueError as e:
            raise ProblemDetailException("Invalid top_k", status=HTTPStatus.BAD_REQUEST, previous=e)

        if top_k < 1:
            raise ProblemDetailException(
                "Invalid top_k",
                status=HTTPStatus.BAD_REQUEST,
                detail="top_k must be greater than 0",
            )

        catalog_id = self._parse_uuid(request.GET.get("catalog_id"), "catalog_id")
        entry_id = self._parse_uuid(request.GET.get("entry_id"), "entry_id")
        page_num = self._parse_int(request.GET.get("page_num"), "page_num")
        if page_num is not None and page_num < 1:
            raise ProblemDetailException(
                "Invalid page_num",
                status=HTTPStatus.BAD_REQUEST,
                detail="page_num must be greater than 0",
            )

        service = EntrySearchService()
        try:
            result = service.search(
                user=request.user,
                query=query,
                mode=mode,
                top_k=top_k,
                catalog_id=catalog_id,
                entry_id=entry_id,
                page_num=page_num,
            )
        except SearchServiceClientError as e:
            raise ProblemDetailException(
                "Search service unavailable",
                status=HTTPStatus.BAD_GATEWAY,
                previous=e,
            )

        return SingleResponse(
            request,
            data=EntrySearchResponseSerializer.model_validate(
                result,
                from_attributes=True,
                context={"shelf_entries": shelf_record_mapping(request.user), "request": request},
            ),
        )

    @staticmethod
    def _parse_uuid(value: str | None, field_name: str) -> UUID | None:
        if not value:
            return None
        try:
            return UUID(value)
        except ValueError as e:
            raise ProblemDetailException(f"Invalid {field_name}", status=HTTPStatus.BAD_REQUEST, previous=e)

    @staticmethod
    def _parse_int(value: str | None, field_name: str) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except ValueError as e:
            raise ProblemDetailException(f"Invalid {field_name}", status=HTTPStatus.BAD_REQUEST, previous=e)
