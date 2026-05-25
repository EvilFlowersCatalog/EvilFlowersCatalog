"""
DataverseClient — HTTP wrapper around the Dataverse REST API.

Replaces the ad-hoc `requests.get(...)` calls scattered through the
former `apps/api/views/dataverse.py`. All upstream calls go through one
place with consistent timeouts, structured error mapping, and a single
auth header path.
"""

import logging
from typing import Any, Dict, Optional

import requests

from apps.dataverse.exceptions import (
    DataverseAuthError,
    DataverseConfigError,
    DataverseError,
    DataverseTransientError,
)

logger = logging.getLogger(__name__)

# Re-export so callers don't need to import the exceptions module too.
__all__ = [
    "DataverseClient",
    "DataverseError",
    "DataverseTransientError",
    "DataverseAuthError",
    "DataverseConfigError",
]

_DEFAULT_TIMEOUT_SECONDS = 60


class DataverseClient:
    """Thin client over the Dataverse REST API.

    Operations:
      - fetch_dataset_metadata(dataset_id) — draft version metadata
      - fetch_dataset_files(dataset_id)    — draft version files
      - update_workflow_whitelist(...)     — admin IP whitelist
      - resume_workflow(invocation_id, base_url) — workflow callback
    """

    def __init__(self, base_url: str, token: str, timeout: int = _DEFAULT_TIMEOUT_SECONDS):
        if not base_url:
            raise DataverseConfigError("Dataverse base URL is required")
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    # ----- internal helpers ------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.token:
            headers["X-Dataverse-key"] = self.token
        if extra:
            headers.update(extra)
        return headers

    def _get(self, path: str) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            return requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise DataverseTransientError(f"GET {url} failed: {exc}") from exc

    def _put(self, path: str, *, data: Any, content_type: str = "application/json") -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = self._headers({"Content-Type": content_type})
        try:
            return requests.put(url, headers=headers, data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DataverseTransientError(f"PUT {url} failed: {exc}") from exc

    def _post(self, url: str, *, data: Any, content_type: str = "application/json") -> requests.Response:
        # `url` is absolute here because workflow-resume URL can live on a
        # different host than the API (DV_WORKFLOW_RESUME_BASE).
        headers = self._headers({"Content-Type": content_type})
        try:
            return requests.post(url, headers=headers, data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DataverseTransientError(f"POST {url} failed: {exc}") from exc

    # ----- public API ------------------------------------------------------

    def require_token(self) -> None:
        if not self.token:
            raise DataverseAuthError("DATAVERSE_API_TOKEN is missing")

    def fetch_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """Returns the `data` block of `/api/datasets/{id}/versions/:draft`.

        Returns an empty dict on non-200 (the legacy view treated missing
        metadata as soft — we keep the same behavior for parity).
        """
        resp = self._get(f"/api/datasets/{dataset_id}/versions/:draft")
        if resp.status_code != 200:
            logger.warning("Dataverse metadata fetch returned %s body=%s", resp.status_code, resp.text[:300])
            return {}
        try:
            return (resp.json() or {}).get("data") or {}
        except ValueError as exc:
            raise DataverseError(f"Invalid JSON from dataset metadata endpoint: {exc}") from exc

    def fetch_dataset_files(self, dataset_id: str) -> list:
        """Returns the file list for the draft version. Raises on non-200."""
        resp = self._get(f"/api/datasets/{dataset_id}/versions/:draft/files")
        if resp.status_code != 200:
            raise DataverseError(f"Dataverse file listing failed: status={resp.status_code} body={resp.text[:500]}")
        try:
            return (resp.json() or {}).get("data") or []
        except ValueError as exc:
            raise DataverseError(f"Invalid JSON from dataset files endpoint: {exc}") from exc

    def get_workflow_whitelist(self) -> requests.Response:
        return self._get("/api/admin/workflows/ip-whitelist")

    def put_workflow_whitelist(self, value: str) -> requests.Response:
        return self._put(
            "/api/admin/workflows/ip-whitelist",
            data=value,
            content_type="text/plain; charset=utf-8",
        )

    def resume_workflow(self, resume_base_url: str, invocation_id: str) -> requests.Response:
        url = f"{resume_base_url.rstrip('/')}/api/workflows/{invocation_id}"
        return self._post(url, data="OK", content_type="text/plain; charset=utf-8")
