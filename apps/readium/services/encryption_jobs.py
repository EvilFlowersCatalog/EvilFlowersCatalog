"""The LCP encryption job as operators see it (#148): one shape for MCP, REST and the job ledger."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.readium.models import EncryptedContent


def _isoformat(value):
    return value.isoformat() if value else None


def is_stale(encrypted_content: EncryptedContent) -> bool:
    if encrypted_content.status != EncryptedContent.EncryptionStatus.PENDING or encrypted_content.reported_at:
        return False
    # Rows dispatched before #99 have no `last_dispatched_at`.
    dispatched_at = encrypted_content.last_dispatched_at or encrypted_content.updated_at
    threshold = timedelta(minutes=settings.EVILFLOWERS_READIUM_ENCRYPTION_STALE_MINUTES)
    return timezone.now() - dispatched_at > threshold


def encryption_job(encrypted_content: EncryptedContent) -> dict:
    """`ENCRYPTION_JOB_SCHEMA` of `apps.mcp.tools.encryption`."""
    acquisition = encrypted_content.acquisition
    entry = acquisition.entry
    return {
        "content_id": encrypted_content.lcp_content_id,
        "status": encrypted_content.status,
        "entry_id": str(entry.pk),
        "entry_title": entry.title,
        "catalog": entry.catalog.url_name,
        "mime": acquisition.mime,
        "input_path": acquisition.content.name if acquisition.content else "",
        "encrypted_path": encrypted_content.encrypted_path,
        "error_message": encrypted_content.error_message,
        "created_at": encrypted_content.created_at.isoformat(),
        "updated_at": encrypted_content.updated_at.isoformat(),
        "encrypted_at": _isoformat(encrypted_content.encrypted_at),
        "task_id": encrypted_content.task_id,
        "trace_id": encrypted_content.trace_id,
        "attempts": encrypted_content.attempts,
        "last_dispatched_at": _isoformat(encrypted_content.last_dispatched_at),
        "reported_at": _isoformat(encrypted_content.reported_at),
        "report": encrypted_content.report,
        "stale": is_stale(encrypted_content),
    }


__all__ = ["encryption_job", "is_stale"]
