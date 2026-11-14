"""The LCP encryption job as the pre-IP-031 surfaces show it (#148): the MCP encryption tools and
`/readium/v1/entries/{id}/encryption` (deprecated adapters over the job facade, Q11).

Operations declare `readium.encrypt` jobs through the facade (`apply`); nothing here sends a task.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.readium.models import EncryptedContent

Status = EncryptedContent.EncryptionStatus

#: `requeue` acts on these; COMPLETED/REGISTERED is `reencrypt`'s job.
REQUEUEABLE = (Status.PENDING, Status.FAILED)


def _isoformat(value):
    return value.isoformat() if value else None


def _stuck_after() -> timedelta:
    from apps.readium.services.statistics import ENCRYPTION_STUCK_AFTER

    return ENCRYPTION_STUCK_AFTER


def is_stale(encrypted_content: EncryptedContent) -> bool:
    """`pending`, no worker report, dispatched longer ago than the stale threshold: almost certainly lost."""
    if encrypted_content.status != Status.PENDING or encrypted_content.reported_at:
        return False
    dispatched_at = encrypted_content.last_dispatched_at or encrypted_content.updated_at
    threshold = timedelta(minutes=settings.EVILFLOWERS_READIUM_ENCRYPTION_STALE_MINUTES)
    return timezone.now() - dispatched_at > threshold


def is_stuck(encrypted_content: EncryptedContent) -> bool:
    """The dashboard's `attention.encryption_stuck`: `pending` and dispatched over an hour ago."""
    if encrypted_content.status != Status.PENDING:
        return False
    since = encrypted_content.last_dispatched_at or encrypted_content.created_at
    return timezone.now() - since > _stuck_after()


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


def jobs_queryset():
    return EncryptedContent.objects.select_related("acquisition__entry__catalog")


def filter_jobs(queryset, *, statuses=None, catalog_id=None, entry_ids=None):
    """`statuses` None means `pending,failed` (the ones that need attention); an empty list means all."""
    statuses = list(REQUEUEABLE) if statuses is None else list(statuses)
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    if catalog_id:
        queryset = queryset.filter(acquisition__entry__catalog_id=catalog_id)
    if entry_ids:
        queryset = queryset.filter(acquisition__entry_id__in=list(entry_ids))
    return queryset


def _declare(encrypted_content, user, spec: dict) -> tuple[bool, dict]:
    """Apply a `readium.encrypt` job; (sent, result fields) from what the facade did."""
    from apps.tasks.application.facade import facade
    from apps.tasks.contract import DispatchError, JobState

    try:
        job = facade.apply("readium.encrypt", encrypted_content.acquisition_id, spec=spec, user=user)
    except DispatchError as error:
        encrypted_content.refresh_from_db()
        return False, {"status": encrypted_content.status, "reason": error.message, "task_id": None, "trace_id": None}
    encrypted_content.refresh_from_db()
    sent = job.state in (JobState.QUEUED, JobState.RUNNING) and encrypted_content.task_id == str(job.current_run_id)
    fields = {"status": encrypted_content.status, "job_id": str(job.pk)}
    if sent:
        fields.update(task_id=encrypted_content.task_id, trace_id=encrypted_content.trace_id)
    else:
        fields.update(task_id=None, trace_id=None, reason=job.reason or encrypted_content.error_message or job.state)
    return sent, fields


def requeue(encrypted_content: EncryptedContent, user) -> dict:
    """Send a stuck (`pending`) or `failed` job again: same content id and output path, new run."""
    result = {"content_id": encrypted_content.lcp_content_id, "entry_title": encrypted_content.acquisition.entry.title}
    if encrypted_content.status not in REQUEUEABLE:
        result.update(
            requeued=False,
            status=encrypted_content.status,
            reason=f"Encryption is already {encrypted_content.status}; refusing to re-encrypt",
        )
        return result
    sent, fields = _declare(encrypted_content, user, {})
    result.update(requeued=sent, **fields)
    return result


def reencrypt(encrypted_content: EncryptedContent, user, *, force: bool = False, dry_run: bool = False) -> dict:
    """Encrypt an already encrypted publication again under the same content id (#98)."""
    from django.utils.translation import gettext as _

    from apps.readium.services.content_encryption_service import ContentEncryptionService

    entry = encrypted_content.acquisition.entry
    active_loans = ContentEncryptionService.active_licenses(encrypted_content).count()
    result = {
        "content_id": encrypted_content.lcp_content_id,
        "entry_id": str(entry.pk),
        "entry_title": entry.title,
        "previous_status": encrypted_content.status,
        "active_loans": active_loans,
    }
    reason = None
    if not entry.read_config("readium_enabled"):
        reason = _("Readium LCP is switched off for this publication.")
    elif not encrypted_content.acquisition.content:
        reason = _("The publication has no file to encrypt.")
    elif active_loans and not force:
        reason = _(
            "%(count)d active loan(s) would break; their readers lose access. Pass `force` to re-encrypt anyway."
        ) % {"count": active_loans}
    if reason:
        result.update(requeued=False, status=encrypted_content.status, reason=reason)
        return result
    if dry_run:
        result.update(
            requeued=True,
            status=encrypted_content.status,
            metadata=ContentEncryptionService.publication_metadata(entry),
        )
        return result
    sent, fields = _declare(encrypted_content, user, {"reencrypt": True, "force": force})
    result.update(requeued=sent, **fields)
    return result


__all__ = [
    "REQUEUEABLE",
    "encryption_job",
    "filter_jobs",
    "is_stale",
    "is_stuck",
    "jobs_queryset",
    "reencrypt",
    "requeue",
]
