"""
DataverseSyncService — orchestrates the Dataverse pre-publish handler.

The old `DataversePrepublishIngest.post` was a 500-line god method
mixing seven concerns. The view (`apps/dataverse/views.py`) now just
parses the payload, authenticates, and delegates to
`DataverseSyncService.sync(payload)`. The service is testable in
isolation against a mocked `DataverseClient`.

This module also handles:
- IP-008 Phase 4 D3: routing via `CatalogRouter` (no more
  "first catalog by id").
- IP-008 Phase 4 D4: symmetric sync. Files removed upstream are
  reported via `dataverse.upstream_file_removed` log lines; deletion
  is opt-in via `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=1`. The
  acquisition type maps to `RESTRICTED_ACCESS` when the upstream
  file is `restricted`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from django.db import transaction

from apps.core.models import Acquisition, Author, Catalog, Entry, EntryAuthor, User
from apps.dataverse.exceptions import DataverseConfigError, DataverseError
from apps.dataverse.services.client import DataverseClient
from apps.dataverse.services.mapper import extract_metadata, map_content_type_to_mime
from apps.dataverse.services.router import CatalogRouter, RoutingDecision

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class SyncSummary:
    catalog_url_name: str
    matched_rule: str
    entry_id: Optional[str] = None
    upserts: int = 0
    updates: int = 0
    drifted_files: List[str] = field(default_factory=list)  # file_urls removed upstream
    actually_deleted: int = 0
    files_seen: int = 0


@dataclass
class PrepublishPayload:
    """Validated workflow payload."""

    dataset_id: str
    global_id: str
    title: Optional[str] = None
    invocation_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PrepublishPayload":
        dataset_id = data.get("dataset_id")
        global_id = data.get("global_id")
        if dataset_id is None or global_id is None:
            raise DataverseError("Missing required fields: dataset_id and global_id")
        return cls(
            dataset_id=str(dataset_id),
            global_id=str(global_id),
            title=data.get("title") or None,
            invocation_id=data.get("invocation_id") or data.get("invocationId"),
        )


class DataverseSyncService:
    """End-to-end pre-publish orchestration."""

    def __init__(
        self,
        client: Optional[DataverseClient] = None,
        router: Optional[CatalogRouter] = None,
        *,
        public_base_url: Optional[str] = None,
    ):
        self.client = client or self._default_client()
        self.router = router or CatalogRouter()
        self.public_base_url = public_base_url or (
            os.getenv("DV_PUBLIC_BASE") or os.getenv("DV_BASE_INTERNAL") or "http://dataverse:8080"
        ).rstrip("/")

    @staticmethod
    def _default_client() -> DataverseClient:
        base = (os.getenv("DV_BASE_INTERNAL") or "http://dataverse:8080").rstrip("/")
        token = (os.getenv("DATAVERSE_API_TOKEN") or "").strip()
        if not token:
            raise DataverseConfigError("DATAVERSE_API_TOKEN is missing")
        return DataverseClient(base, token)

    # ----- public API ------------------------------------------------------

    def sync(self, payload: PrepublishPayload) -> SyncSummary:
        """Run the full pre-publish flow.

        Returns a `SyncSummary` for logging/observability. Raises
        `DataverseError` on protocol / config failures; the view maps
        these to RFC 7807 responses.
        """
        decision = self.router.resolve(dataset_id=payload.dataset_id, global_id=payload.global_id)
        catalog = decision.catalog
        logger.info(
            "Dataverse sync starting dataset_id=%s global_id=%s catalog=%s matched_rule=%s",
            payload.dataset_id,
            payload.global_id,
            catalog.url_name,
            decision.matched_rule.value,
        )

        system_user = self._resolve_system_user()

        dataset_data = self.client.fetch_dataset_metadata(payload.dataset_id)
        files = self.client.fetch_dataset_files(payload.dataset_id)
        metadata = extract_metadata(
            dataset_data,
            files,
            title_override=payload.title,
            global_id=payload.global_id,
        )

        summary = SyncSummary(
            catalog_url_name=catalog.url_name,
            matched_rule=decision.matched_rule.value,
            files_seen=len(files),
        )

        with transaction.atomic():
            entry, created, updated = self._upsert_entry(catalog, system_user, payload, metadata)
            summary.entry_id = str(entry.pk)
            if created:
                summary.upserts = 1
            if updated:
                summary.updates = 1

            self._sync_authors(catalog, entry, metadata)
            self._sync_acquisitions(entry, files, summary)

        logger.info(
            "Dataverse sync summary: entry=%s catalog=%s matched_rule=%s upserts=%d "
            "updates=%d files_seen=%d would_delete=%d actually_deleted=%d",
            summary.entry_id,
            summary.catalog_url_name,
            summary.matched_rule,
            summary.upserts,
            summary.updates,
            summary.files_seen,
            len(summary.drifted_files),
            summary.actually_deleted,
        )
        return summary

    # ----- internal --------------------------------------------------------

    @staticmethod
    def _resolve_system_user() -> User:
        user = User.objects.filter(is_superuser=True).first()
        if user is None:
            raise DataverseConfigError("No superuser available to own Dataverse-imported entries")
        return user

    def _upsert_entry(
        self,
        catalog: Catalog,
        system_user: User,
        payload: PrepublishPayload,
        metadata: Dict[str, Any],
    ) -> tuple[Entry, bool, bool]:
        """Insert or update the entry identified by `dataverse_pid`.

        Returns `(entry, created, updated)`.
        """
        entry = Entry.objects.filter(
            catalog=catalog,
            identifiers__dataverse_pid=payload.global_id,
        ).first()

        identifiers: Dict[str, Any] = {
            "dataverse_pid": payload.global_id,
            "dataverse_dataset_id": payload.dataset_id,
        }
        if metadata.get("doi"):
            identifiers["doi"] = metadata["doi"]

        if entry is None:
            entry = Entry.objects.create(
                creator=system_user,
                catalog=catalog,
                title=metadata.get("title") or f"Dataverse Dataset {payload.global_id}",
                summary=metadata.get("summary"),
                content=metadata.get("content"),
                language=metadata.get("language"),
                publisher=metadata.get("publisher"),
                published_at=metadata.get("published_at"),
                citation=metadata.get("citation"),
                identifiers=identifiers,
            )
            logger.info("Created new Dataverse entry %s in catalog %s", entry.pk, catalog.url_name)
            return entry, True, False

        # Update path
        merged_identifiers = {**(entry.identifiers or {}), **identifiers}

        updated = False
        for field_name, new_value in (
            ("title", metadata.get("title")),
            ("summary", metadata.get("summary")),
            ("content", metadata.get("content")),
            ("publisher", metadata.get("publisher")),
            ("published_at", metadata.get("published_at")),
            ("citation", metadata.get("citation")),
            ("language", metadata.get("language")),
        ):
            if new_value is not None and getattr(entry, field_name) != new_value:
                setattr(entry, field_name, new_value)
                updated = True

        if merged_identifiers != (entry.identifiers or {}):
            entry.identifiers = merged_identifiers
            updated = True

        if updated:
            entry.save()
            logger.info("Updated Dataverse entry %s in catalog %s", entry.pk, catalog.url_name)
        return entry, False, updated

    @staticmethod
    def _sync_authors(catalog: Catalog, entry: Entry, metadata: Dict[str, Any]) -> None:
        author_dicts = metadata.get("authors") or []
        if not author_dicts:
            # Legacy parity: do NOT clear authors when upstream sent none —
            # avoids wiping a manually curated list.
            return

        entry.authors.clear()
        for idx, author_data in enumerate(author_dicts):
            author, _ = Author.objects.get_or_create(
                catalog=catalog,
                name=author_data.get("name", ""),
                surname=author_data.get("surname", ""),
            )
            EntryAuthor.objects.get_or_create(entry=entry, author=author, defaults={"position": idx})

    def _sync_acquisitions(
        self,
        entry: Entry,
        files: List[Dict[str, Any]],
        summary: SyncSummary,
    ) -> None:
        """Upsert one Acquisition per upstream file.

        IP-008 Phase 4 D4: also detects upstream-removed files. We log
        each removal at WARNING with structured fields so a future alert
        pipeline can pick it up. Actual deletion only happens when
        `EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED=1`.
        """
        seen_urls: set[str] = set()

        for item in files:
            data_file = item.get("dataFile") or {}
            datafile_id = data_file.get("id")
            if not datafile_id:
                continue

            content_type = data_file.get("contentType")
            restricted = bool(data_file.get("restricted") or item.get("restricted"))
            browser_url = f"{self.public_base_url}/api/access/datafile/{datafile_id}"
            seen_urls.add(browser_url)

            mime_type = map_content_type_to_mime(content_type)
            target_relation = (
                Acquisition.AcquisitionType.RESTRICTED_ACCESS
                if restricted
                else Acquisition.AcquisitionType.OPEN_ACCESS
            )

            existing = Acquisition.objects.filter(entry=entry, file_url=browser_url).first()
            if existing is None:
                Acquisition.objects.create(
                    entry=entry,
                    mime=mime_type,
                    file_url=browser_url,
                    relation=target_relation,
                    content=None,
                )
                continue

            updated_fields: List[str] = []
            if existing.mime != mime_type:
                existing.mime = mime_type
                updated_fields.append("mime")
            if existing.relation != target_relation:
                existing.relation = target_relation
                updated_fields.append("relation")
            if updated_fields:
                existing.save(update_fields=updated_fields + ["updated_at"])

        # Drift detection: acquisitions on this entry that look like
        # Dataverse-backed (file_url under our public base) but weren't
        # in the upstream listing this round.
        dataverse_prefix = f"{self.public_base_url}/api/access/datafile/"
        orphan_qs = Acquisition.objects.filter(entry=entry, file_url__startswith=dataverse_prefix).exclude(
            file_url__in=seen_urls
        )
        destroy_drift = _env_flag("EVILFLOWERS_DATAVERSE_SYNC_DELETE_REMOVED", default=False)
        for orphan in orphan_qs:
            summary.drifted_files.append(orphan.file_url)
            logger.warning(
                "event=dataverse.upstream_file_removed acquisition_id=%s entry_id=%s "
                "catalog_url_name=%s file_url=%s destructive=%s",
                orphan.pk,
                entry.pk,
                entry.catalog.url_name,
                orphan.file_url,
                destroy_drift,
            )
            if destroy_drift:
                orphan.delete()
                summary.actually_deleted += 1
