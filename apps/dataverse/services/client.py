"""
DataverseClient — HTTP wrapper around the Dataverse REST API.

Replaces the ad-hoc `requests.get(...)` calls scattered through the
former `apps/api/views/dataverse.py`. All upstream calls go through one
place with consistent timeouts, structured error mapping, and a single
auth header path.
"""

import json
import logging
from typing import Any, Dict, Optional

import requests

from apps.dataverse.exceptions import (
    DataverseAuthError,
    DataverseConfigError,
    DataverseError,
    DataverseNotFoundError,
    DataverseStaleError,
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
    "DataverseNotFoundError",
    "DataverseStaleError",
]

_DEFAULT_TIMEOUT_SECONDS = 60


class DataverseClient:
    """Thin client over the Dataverse REST API.

    Operations:
      - fetch_dataset_metadata(dataset_id) — draft version metadata
      - fetch_dataset_files(dataset_id)    — draft version files
      - update_workflow_whitelist(...)     — admin IP whitelist
      - resume_workflow(invocation_id, base_url) — workflow callback
      - IP-034: get_version / get_version_files / get_dataset(_by_pid) / list_versions / search_datasets /
        edit_metadata / delete_metadata / publish / create_dataset (strict: raise on failure)
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

    # ----- IP-034: strict reads and writes -----------------------------------------------------------------
    # Unlike the legacy soft reads above, these raise on anything but success: a merge must never read a failed
    # request as "the dataset has no metadata".

    def _data(self, resp: requests.Response, what: str) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            try:
                return (resp.json() or {}).get("data")
            except ValueError as exc:
                raise DataverseError(f"Invalid JSON from {what}: {exc}") from exc
        body = resp.text[:500] if resp.text else ""
        if status == 404:
            raise DataverseNotFoundError(f"{what}: not found ({body})")
        if status in (401, 403):
            raise DataverseAuthError(f"{what}: status={status} body={body}")
        if status >= 500 or status == 429:
            raise DataverseTransientError(f"{what}: status={status} body={body}")
        raise DataverseError(f"{what}: status={status} body={body}")

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = self._headers({"Content-Type": "application/json"} if json_body is not None else None)
        try:
            return requests.request(
                method,
                url,
                headers=headers,
                params=params,
                data=json.dumps(json_body) if json_body is not None else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DataverseTransientError(f"{method} {url} failed: {exc}") from exc

    def get_version(self, dataset_id: str, version: str = ":latest-published") -> Dict[str, Any]:
        """One version of a dataset (IP-034 Q4: inbound reads `:latest-published`; validation reads `:draft`)."""
        return self._data(
            self._request("GET", f"/api/datasets/{dataset_id}/versions/{version}"), f"dataset {dataset_id} {version}"
        )

    def get_version_by_pid(self, pid: str, version: str = ":latest-published") -> Dict[str, Any]:
        """One version of the dataset with this persistent id."""
        return self._data(
            self._request("GET", f"/api/datasets/:persistentId/versions/{version}", params={"persistentId": pid}),
            f"dataset {pid} {version}",
        )

    def get_version_files(self, dataset_id: str, version: str = ":latest-published") -> list:
        return (
            self._data(
                self._request("GET", f"/api/datasets/{dataset_id}/versions/{version}/files"),
                f"files of dataset {dataset_id} {version}",
            )
            or []
        )

    def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """The dataset with its `latestVersion` (a draft when one is open) and, since 6.4, `datasetType`."""
        return self._data(self._request("GET", f"/api/datasets/{dataset_id}"), f"dataset {dataset_id}")

    def get_dataset_by_pid(self, pid: str) -> Dict[str, Any]:
        return self._data(
            self._request("GET", "/api/datasets/:persistentId/", params={"persistentId": pid}), f"dataset {pid}"
        )

    def list_versions(self, pid: str, include_deaccessioned: bool = True) -> list:
        params = {"persistentId": pid}
        if include_deaccessioned:
            params["includeDeaccessioned"] = "true"
        return (
            self._data(
                self._request("GET", "/api/datasets/:persistentId/versions", params=params), f"versions of {pid}"
            )
            or []
        )

    def search_datasets(self, *, start: int = 0, per_page: int = 100, subtree: Optional[str] = None) -> Dict:
        """Datasets newest change first (`sort=date`); the reconcile pages until it passes its cursor."""
        params = {"q": "*", "type": "dataset", "sort": "date", "order": "desc", "start": start, "per_page": per_page}
        if subtree:
            params["subtree"] = subtree
        return self._data(self._request("GET", "/api/search", params=params), "search") or {}

    def edit_metadata(self, pid: str, fields: list, source_last_update_time: Optional[str]) -> Dict[str, Any]:
        """`editMetadata?replace=true` with optimistic locking. `sourceLastUpdateTime` is passed back verbatim as
        Dataverse returned it. A refusal for a stale timestamp raises `DataverseStaleError`."""
        body: Dict[str, Any] = {"fields": fields}
        if source_last_update_time:
            body["sourceLastUpdateTime"] = source_last_update_time
        resp = self._request(
            "PUT",
            "/api/datasets/:persistentId/editMetadata",
            params={"persistentId": pid, "replace": "true"},
            json_body=body,
        )
        if self._is_stale(resp):
            raise DataverseStaleError(f"dataset {pid} changed since {source_last_update_time}: {resp.text[:300]}")
        return self._data(resp, f"editMetadata {pid}") or {}

    @staticmethod
    def _is_stale(resp: requests.Response) -> bool:
        """The one place that recognises an optimistic-locking refusal (status and wording unverified live)."""
        if resp.status_code == 409:
            return True
        if resp.status_code == 400:
            text = (resp.text or "").lower()
            return "sourcelastupdatetime" in text or "lastupdatetime" in text or "has been modified" in text
        return False

    def delete_metadata(self, pid: str, fields: list) -> Dict[str, Any]:
        """Remove field values (`deleteMetadata`): `editMetadata` cannot set a field to empty."""
        resp = self._request(
            "PUT",
            "/api/datasets/:persistentId/deleteMetadata",
            params={"persistentId": pid},
            json_body={"fields": fields},
        )
        return self._data(resp, f"deleteMetadata {pid}") or {}

    def publish(self, pid: str, release_type: str = "updatecurrent") -> Dict[str, Any]:
        """`updatecurrent` (superuser only) folds the draft into the current version without a new number."""
        resp = self._request(
            "POST", "/api/datasets/:persistentId/actions/:publish", params={"persistentId": pid, "type": release_type}
        )
        return self._data(resp, f"publish {pid} type={release_type}") or {}

    def create_dataset(self, alias: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """`POST /api/dataverses/{alias}/datasets`; answers `{"id": …, "persistentId": …}`."""
        return self._data(
            self._request("POST", f"/api/dataverses/{alias}/datasets", json_body=payload), f"create in {alias}"
        )

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
