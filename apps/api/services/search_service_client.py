import logging
from typing import Any, Optional

import requests
from django.conf import settings


logger = logging.getLogger("apps.api.services.search_service_client")


class SearchServiceClientError(Exception):
    pass


class SearchServiceClient:
    def __init__(self):
        self.base_url = settings.EVILFLOWERS_SEARCH_SERVICE_URL.rstrip("/")
        self.timeout = settings.EVILFLOWERS_SEARCH_SERVICE_TIMEOUT

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def search_elasticsearch(
        self,
        query: str,
        top_k: int = 10,
        document_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "top_k": top_k,
            "document_id": document_id,
        }
        return self._request("POST", "/search/elasticsearch", json=payload)

    def search_semantic(
        self,
        query: str,
        top_k: int = 10,
        document_id: Optional[str] = None,
        page_num: Optional[int] = None,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "top_k": top_k,
            "document_id": document_id,
            "page_num": page_num,
        }
        return self._request("POST", "/search/semantic", json=payload)

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = requests.request(
                method=method,
                url=f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.exception("Search service request failed: method=%s path=%s", method, path)
            raise SearchServiceClientError(str(e)) from e

