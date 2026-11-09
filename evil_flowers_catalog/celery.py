import logging.config
import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    beat_init,
    setup_logging,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
)
from django.conf import settings

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evil_flowers_catalog.settings.development")

app = Celery("evil_flowers_catalog")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

app.conf.beat_schedule = {}


# -- logging and telemetry (IP-019) ------------------------------------------


@setup_logging.connect
def _use_django_logging(**kwargs):
    """Keep Celery from replacing the root handlers with its plain-text format.

    With a receiver on this signal Celery configures no logging of its own; the
    worker and beat log through `settings.LOGGING` (JSON lines) like gunicorn.
    `warnings` go through logging too, as Django's own logging setup does.
    """
    logging.config.dictConfig(settings.LOGGING)
    logging.captureWarnings(True)


def _is_prefork(worker) -> bool:
    pool = getattr(worker, "pool_cls", None)
    return "prefork" in f"{getattr(pool, '__module__', '')}.{getattr(pool, '__name__', pool)}"


@worker_init.connect
def _discover_collector(sender=None, **kwargs):
    """Main worker process: look for the collector once, before the pool forks.

    The children inherit the discovered endpoint (or the verdict that there is
    none) through the environment, so they neither probe nor log again. With a
    non-forking pool the tasks run here, so this process is the one to set up.
    """
    from evil_flowers_catalog import telemetry

    telemetry.discover_endpoint()
    if sender is not None and not _is_prefork(sender):
        telemetry.setup("celery-worker")


@worker_process_init.connect
def _setup_telemetry_in_child(**kwargs):
    from evil_flowers_catalog import telemetry

    telemetry.setup("celery-worker")


@worker_process_shutdown.connect
def _flush_telemetry_in_child(**kwargs):
    # Prefork children leave through `os._exit()`, which skips `atexit`.
    from evil_flowers_catalog import telemetry

    telemetry.shutdown()


@beat_init.connect
def _setup_telemetry_in_beat(**kwargs):
    # Beat turns SIGTERM into SystemExit itself, so `atexit` flushes on exit.
    from evil_flowers_catalog import telemetry

    telemetry.setup("celery-beat")


def _cron(expression: str) -> crontab:
    minute, hour, day_of_month, month_of_year, day_of_week = expression.split()
    return crontab(
        minute=minute, hour=hour, day_of_month=day_of_month, month_of_year=month_of_year, day_of_week=day_of_week
    )


if settings.EVILFLOWERS_BACKUP_DESTINATION and settings.EVILFLOWERS_BACKUP_SCHEDULE:
    app.conf.beat_schedule["backup"] = {
        "task": "apps.tasks.tasks.backup",
        "schedule": _cron(settings.EVILFLOWERS_BACKUP_SCHEDULE),
    }

# #30: delete storage directories no row owns, older than EVILFLOWERS_ORPHAN_CLEANUP_MIN_AGE_HOURS.
if settings.EVILFLOWERS_ORPHAN_CLEANUP_SCHEDULE:
    app.conf.beat_schedule["files-cleanup-orphans"] = {
        "task": "apps.tasks.tasks.cleanup_orphans",
        "schedule": _cron(settings.EVILFLOWERS_ORPHAN_CLEANUP_SCHEDULE),
    }

# IP-003: reservation queue + lifecycle notifications.
app.conf.beat_schedule["readium-sweep-unclaimed-reservations"] = {
    "task": "apps.readium.tasks.sweep_unclaimed_reservations",
    "schedule": crontab(minute="*"),
}
app.conf.beat_schedule["readium-notify-expiring-licenses"] = {
    "task": "apps.readium.tasks.notify_expiring_licenses",
    "schedule": crontab(hour=6, minute=0),
}
# IP-009 Phase 2: transition naturally-expired licenses out of
# READY/ACTIVE so `can_user_borrow` no longer rejects re-borrow.
app.conf.beat_schedule["readium-expire-lapsed-licenses"] = {
    "task": "apps.readium.tasks.expire_lapsed_licenses",
    "schedule": crontab(minute="*/5"),
}
# IP-011 Phase 5: nudge users whose claim window is about to close.
# Dedups via NotificationLog so a single reminder fires per AVAILABLE cycle.
app.conf.beat_schedule["readium-reservation-claim-reminder"] = {
    "task": "apps.readium.tasks.reservation_claim_reminder_sweep",
    "schedule": crontab(minute="*/15"),
}
# IP-016: drop personal activity history older than the retention window.
app.conf.beat_schedule["core-prune-user-activity"] = {
    "task": "apps.tasks.tasks.prune_user_activity",
    "schedule": crontab(hour=3, minute=30),
}
