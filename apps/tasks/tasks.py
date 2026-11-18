import json
import logging
from io import StringIO

from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task(acks_late=False, reject_on_worker_lost=False, time_limit=settings.EVILFLOWERS_BACKUP_TIME_LIMIT)
def backup():
    """Nightly backup, on the maintenance queue (#193).

    Acked on receipt whatever `CELERY_TASK_ACKS_LATE` says: a backup killed mid-run must not come back
    after the 12 h visibility timeout and run during the day. The next night's schedule retries it.
    Its own hard limit, `EVILFLOWERS_BACKUP_TIME_LIMIT` (#197): it takes ~11 min on dev and hours on prod, and grows
    with the library. Its job kind carries the same value, so the sweeper does not call it `lost` sooner.
    """
    if settings.EVILFLOWERS_BACKUP_DESTINATION:
        call_command(
            "backup",
            destination=settings.EVILFLOWERS_BACKUP_DESTINATION,
        )


@shared_task
def cleanup_orphans() -> dict:
    """Remove storage directories no row owns any more, once they are old enough (#30).

    Deletions already remove their files (`apps.files.signals`); this catches what a failed
    cleanup or an older release left behind. Recent orphans are kept: their row may not be
    committed yet.
    """
    out = StringIO()
    call_command(
        "storage_audit",
        delete_orphans=True,
        skip_missing=True,
        min_age_hours=settings.EVILFLOWERS_ORPHAN_CLEANUP_MIN_AGE_HOURS,
        format="json",
        stdout=out,
    )
    orphans = json.loads(out.getvalue())["orphans"]
    deleted = [orphan["path"] for orphan in orphans if orphan.get("deleted")]
    kept = [orphan["path"] for orphan in orphans if orphan.get("recent")]
    for path in deleted:
        logger.info("storage.orphan_deleted path=%s", path)
    return {"deleted": len(deleted), "kept_recent": len(kept)}


@shared_task
def cleanup_personalised_copies() -> dict:
    """Delete stored personalised copies no reader has used for a while (#70)."""
    from apps.files.personalised import cleanup_idle

    return {"deleted": cleanup_idle()}


@shared_task
def build_personalised_base(acquisition_id: str) -> dict:
    """Normalise one acquisition's file into the shared base of its readers' copies (#214), off the request path."""
    from apps.core.models import Acquisition
    from apps.files.personalised import build_base

    acquisition = Acquisition.objects.select_related("entry").filter(pk=acquisition_id).first()
    if acquisition is None:
        return {"built": False, "reason": "missing"}
    return {"built": build_base(acquisition)}


@shared_task
def sync_ldap_profiles() -> dict:
    """IP-025: import e-mails (and names) of LDAP users who have not logged in since the mapping changed."""
    from apps.core.models import AuthSource
    from apps.core.services.ldap_directory import DirectorySyncError, sync_users

    summaries = {}
    for source in AuthSource.objects.filter(driver=AuthSource.Driver.LDAP, is_active=True):
        try:
            summaries[source.name] = sync_users(source).summary
        except DirectorySyncError as error:
            logger.warning("ldap.sync_failed auth_source=%s error=%s", source.name, error)
            summaries[source.name] = {"error": str(error)}
    return summaries


@shared_task
def fail_stale_extraction_runs() -> int:
    """Fail extraction runs stuck in `running`, so a backfill can retry them."""
    from apps.extraction.services import fail_stale_runs

    if not settings.EVILFLOWERS_EXTRACTION_STALE_HOURS:
        return 0
    return fail_stale_runs(settings.EVILFLOWERS_EXTRACTION_STALE_HOURS)


@shared_task
def prune_user_activity() -> int:
    from apps.core.services.activity import ActivityService

    return ActivityService.prune()


@shared_task
def prune_read_events() -> int:
    """#194: drop read events past `EVILFLOWERS_READ_EVENT_RETENTION_DAYS` (at least the 400-day statistics span)."""
    from apps.core.services.reads import ReadService

    return ReadService.prune()


@shared_task
def prune_content_disclosures() -> int:
    """#175: drop disclosure log rows past their policy's retention (never inside a budget window)."""
    from apps.content.disclosure import prune

    return prune()


@shared_task
def reconcile_jobs() -> dict:
    """IP-031: the reconciler's tick — every job with something to do (blocked, retry due, cancel) moves on.

    Also applies outcome files whose run's terminal event never reached the listener (#169).
    """
    from apps.tasks.application.facade import facade

    return {"reconciled": facade.reconcile_due(), "outcomes": facade.apply_pending_outcomes()}


@shared_task
def sweep_jobs() -> dict:
    """IP-031: mark runs `lost` when no event arrived for their time limit plus the grace."""
    from apps.tasks.infrastructure.maintenance import sweep_lost_runs

    return {"lost": sweep_lost_runs()}


@shared_task
def prune_jobs() -> dict:
    """IP-031: delete finished runs past their retention; a job's current run stays, jobs stay."""
    from apps.tasks.infrastructure.maintenance import prune_runs

    return {"deleted": prune_runs()}


@shared_task
def sync_jobs() -> dict:
    """IP-031: create or refresh the jobs of every subject providers know (`JobDefinition.discover`)."""
    from apps.tasks.application.facade import facade

    return {"observed": facade.discover()}
