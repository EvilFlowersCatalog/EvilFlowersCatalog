from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from apps.api.serializers import Serializer
from apps.harvest.models import HarvestRecord, HarvestSource


def record_counts(source: HarvestSource, *, dry_run: bool = False) -> Dict[str, int]:
    from django.db.models import Count

    rows = (
        HarvestRecord.objects.filter(source=source, dry_run=dry_run)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )
    return {row["status"]: row["count"] for row in rows}


class HarvestSourceSerializer:
    class Base(Serializer):
        id: UUID
        slug: str
        name: str
        adapter: str
        config: Dict[str, Any]
        base_url: Optional[str] = None
        metadata_prefix: str
        set_spec: Optional[str] = None
        system_key: str
        target_catalog_id: UUID
        mode: HarvestSource.Mode
        field_policy: Dict[str, str]
        schedule: str
        enabled: bool
        oai_reexpose: bool
        cursor: Optional[str] = None
        #: A `marc_file` source has a file to run on.
        has_upload: bool
        last_run_at: Optional[datetime] = None
        last_report: Optional[Dict[str, Any]] = None
        creator_id: Optional[UUID] = None
        created_at: datetime
        updated_at: datetime

    class Detailed(Base):
        #: Mirror rows of the last real runs, per status.
        record_counts: Dict[str, int] = {}
        #: Rows of the last dry run, per status.
        dry_run_counts: Dict[str, int] = {}

        @classmethod
        def of(cls, source: HarvestSource) -> "HarvestSourceSerializer.Detailed":
            return cls.model_validate(source).model_copy(
                update={"record_counts": record_counts(source), "dry_run_counts": record_counts(source, dry_run=True)}
            )


class HarvestRecordSerializer:
    class Base(Serializer):
        id: UUID
        source_id: UUID
        record_id: str
        datestamp: Optional[datetime] = None
        deleted: bool
        status: HarvestRecord.Status
        matched_on: Optional[str] = None
        message: Optional[str] = None
        dry_run: bool
        entry_id: Optional[UUID] = None
        source_record: Dict[str, Any]
        sync_state: Optional[str] = None
        remote_version: Optional[str] = None
        local_version: Optional[datetime] = None
        synced_at: Optional[datetime] = None
        created_at: datetime
        updated_at: datetime


class AdapterTypeSerializer(Serializer):
    """A registered connector, for the portal's source form."""

    name: str
    title: str
    description: str
    #: `read` always; `write` (push), `validate`, `link` (record URLs) when the connector offers them.
    capabilities: List[str]
    #: JSON Schema of the source's `config`, or null.
    config_schema: Optional[Dict[str, Any]] = None
    needs_base_url: bool
    needs_upload: bool
    runnable: bool
    #: `Entry.identifiers` keys the connector owns (a repository's persistent id), valid as a source's `system_key`
    #: next to the external systems (#210).
    system_keys: List[str] = []


class ConflictSerializer(Serializer):
    id: UUID
    source_id: UUID
    record_id: UUID
    entry_id: UUID
    field: str
    local_value: Any = None
    remote_value: Any = None
    local_at: Optional[datetime] = None
    remote_at: Optional[datetime] = None
    winner: str
    acknowledged: bool
    acknowledged_by_id: Optional[UUID] = None
    acknowledged_at: Optional[datetime] = None
    created_at: datetime


class MatchSuggestionSerializer(Serializer):
    """#207: the decider's suggested entry for a record no identifier matched; a librarian accepts or rejects."""

    id: UUID
    source_id: UUID
    #: The mirror row (`GET harvest/sources/{source_id}/records`).
    record_id: UUID
    #: The source's own id of the record.
    source_record_id: str
    #: What the source said: title, authors, published, publisher, language, identifiers.
    source_record: Dict[str, Any]
    entry_id: UUID
    entry_title: str
    #: Probability of the chosen entry (uncalibrated until knowledge#65).
    p: Optional[float] = None
    #: Entry ids the decider chose among.
    candidates: List[str] = []
    decider_model: str
    status: str
    decided_by_id: Optional[UUID] = None
    decided_at: Optional[datetime] = None
    created_at: datetime


class EntrySourceSyncSerializer(Serializer):
    """One source an entry is linked to (any mode), as `GET entries/{id}/sync` lists it."""

    source_id: UUID
    slug: str
    name: str
    adapter: str
    mode: str
    record_id: str
    record_url: Optional[str] = None
    sync_state: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    remote_version: Optional[str] = None
    local_version: Optional[datetime] = None
    message: Optional[str] = None
    open_conflicts: int = 0


class EntrySyncSerializer(Serializer):
    sources: List[EntrySourceSyncSerializer]


class ExternalSystemSerializer(Serializer):
    key: str
    name: str
    kind: str
    marc_org_code: Optional[str] = None
    record_url_template: Optional[str] = None


class SourceProbeSerializer(Serializer):
    """What `POST harvest/sources/{id}/test` found; nothing is written."""

    identify: Optional[Dict[str, Optional[str]]] = None
    metadata_formats: List[Dict[str, Optional[str]]] = []
    sets: List[Dict[str, Optional[str]]] = []
    sample: Optional[Dict[str, Any]] = None
    errors: List[str] = []


__all__ = [
    "AdapterTypeSerializer",
    "ConflictSerializer",
    "EntrySourceSyncSerializer",
    "EntrySyncSerializer",
    "ExternalSystemSerializer",
    "HarvestRecordSerializer",
    "HarvestSourceSerializer",
    "MatchSuggestionSerializer",
    "SourceProbeSerializer",
    "record_counts",
]
