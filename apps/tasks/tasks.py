import json
import logging
from io import StringIO

from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task
def backup():
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
def prune_user_activity() -> int:
    from apps.core.services.activity import ActivityService

    return ActivityService.prune()
