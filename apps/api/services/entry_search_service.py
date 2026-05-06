from typing import Any, Optional
from uuid import UUID

from django.db.models import Case, IntegerField, Q, When

from apps.api.services.search_service_client import SearchServiceClient
from apps.core.models import Entry


class EntrySearchService:
    def __init__(self, client: Optional[SearchServiceClient] = None):
        self.client = client or SearchServiceClient()

    def search(
        self,
        *,
        user,
        query: str,
        mode: str = "elasticsearch",
        top_k: int = 10,
        catalog_id: Optional[UUID] = None,
        entry_id: Optional[UUID] = None,
        page_num: Optional[int] = None,
    ) -> dict[str, Any]:
        document_id = str(entry_id) if entry_id else None

        if mode == "semantic":
            response = self.client.search_semantic(
                query=query,
                top_k=top_k,
                document_id=document_id,
                page_num=page_num,
            )
        else:
            response = self.client.search_elasticsearch(
                query=query,
                top_k=top_k,
                document_id=document_id,
            )

        raw_results = response.get("results", [])
        document_ids = self._extract_document_ids(raw_results)
        entries = self._fetch_entries(user=user, document_ids=document_ids, catalog_id=catalog_id)
        entry_mapping = {str(entry.pk): entry for entry in entries}

        results = []
        for item in raw_results:
            mapped_entry = entry_mapping.get(str(item.get("document_id")))
            if not mapped_entry:
                continue

            results.append(
                {
                    "entry": mapped_entry,
                    "score": item.get("score"),
                    "chunk_id": item.get("chunk_id"),
                    "highlight": item.get("highlight"),
                    "source": item.get("source"),
                }
            )

        return {
            "search_type": response.get("search_type", mode),
            "query": response.get("query", query),
            "total_results": len(results),
            "results": results,
        }

    @staticmethod
    def _extract_document_ids(results: list[dict[str, Any]]) -> list[UUID]:
        document_ids = []
        for item in results:
            document_id = item.get("document_id")
            try:
                document_uuid = UUID(str(document_id))
            except (TypeError, ValueError):
                continue

            if document_uuid in document_ids:
                continue
            document_ids.append(document_uuid)
        return document_ids

    @staticmethod
    def _fetch_entries(*, user, document_ids: list[UUID], catalog_id: Optional[UUID]) -> list[Entry]:
        if not document_ids:
            return []

        queryset = (
            Entry.objects.filter(pk__in=document_ids)
            .select_related("language")
            .prefetch_related(
                "entry_authors__author",
                "categories",
                "feeds",
                "acquisitions",
            )
        )

        if catalog_id:
            queryset = queryset.filter(catalog_id=catalog_id)

        if user.is_superuser:
            filtered = queryset
        elif user.is_authenticated:
            filtered = queryset.filter(Q(catalog__is_public=True) | Q(catalog__user_catalogs__user=user)).distinct()
        else:
            filtered = queryset.filter(catalog__is_public=True)

        ordering = Case(
            *[When(pk=document_id, then=position) for position, document_id in enumerate(document_ids)],
            output_field=IntegerField(),
        )
        return list(filtered.order_by(ordering))
