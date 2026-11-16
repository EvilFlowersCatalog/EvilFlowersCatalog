"""Per entry, the sources it is linked to and how their sync stands (`GET entries/{id}/sync`, MCP
`get_entry_sync`). Every mode is listed, so the portal can also show link-only catalogue records."""

from __future__ import annotations

from typing import Optional

from apps.core.identifiers import external_system
from apps.core.models import Entry
from apps.harvest.adapters import registry
from apps.harvest.models import HarvestConflict, HarvestRecord, HarvestSource


def record_url(source: HarvestSource, record_id: str) -> Optional[str]:
    """The connector's landing page of the record, else the configured external system's URL template."""
    connector = registry.get(source.adapter)
    url = connector.record_url(source, record_id) if connector is not None else None
    if url:
        return url
    system = external_system(source.system_key)
    return system.record_url(record_id) if system is not None else None


def entry_sync(entry: Entry) -> list[dict]:
    identifiers = entry.identifiers or {}
    sources = [
        source
        for source in HarvestSource.objects.filter(target_catalog_id=entry.catalog_id).order_by("slug")
        if identifiers.get(source.system_key)
    ]
    result = []
    for source in sources:
        record_id = identifiers[source.system_key]
        row = HarvestRecord.objects.filter(source=source, record_id=record_id, dry_run=False).first()
        result.append(
            {
                "source_id": source.pk,
                "slug": source.slug,
                "name": source.name,
                "adapter": source.adapter,
                "mode": source.mode,
                "record_id": record_id,
                "record_url": record_url(source, record_id),
                "sync_state": row.sync_state if row else None,
                "last_synced_at": (row.synced_at or row.updated_at) if row else None,
                "remote_version": row.remote_version if row else None,
                "local_version": row.local_version if row else None,
                "message": row.message if row else None,
                "open_conflicts": HarvestConflict.objects.filter(
                    source=source, entry=entry, acknowledged=False
                ).count(),
            }
        )
    return result


__all__ = ["entry_sync", "record_url"]
