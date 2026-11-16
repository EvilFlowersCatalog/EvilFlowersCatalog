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
    from evil_flowers_catalog import profiling, telemetry

    telemetry.discover_endpoint()
    if sender is not None and not _is_prefork(sender):
        profiling.setup("celery-worker")
        telemetry.setup("celery-worker")


@worker_process_init.connect
def _setup_telemetry_in_child(**kwargs):
    # Profiling first: telemetry links spans to profiles only when it runs (#111).
    from evil_flowers_catalog import profiling, telemetry

    profiling.setup("celery-worker")
    telemetry.setup("celery-worker")


@worker_process_shutdown.connect
def _flush_telemetry_in_child(**kwargs):
    # Prefork children leave through `os._exit()`, which skips `atexit`.
    from evil_flowers_catalog import profiling, telemetry

    telemetry.shutdown()
    profiling.shutdown()


@beat_init.connect
def _setup_telemetry_in_beat(**kwargs):
    # Beat turns SIGTERM into SystemExit itself, so `atexit` flushes on exit.
    from evil_flowers_catalog import profiling, telemetry

    profiling.setup("celery-beat")
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

# #70: delete personalised copies no reader has used for EVILFLOWERS_PERSONALISED_COPY_IDLE_HOURS.
app.conf.beat_schedule["files-cleanup-personalised-copies"] = {
    "task": "apps.tasks.tasks.cleanup_personalised_copies",
    "schedule": crontab(minute=17),
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
# IP-025: import LDAP e-mails of users who have not logged in since the mapping changed.
if settings.EVILFLOWERS_LDAP_SYNC_SCHEDULE:
    app.conf.beat_schedule["core-sync-ldap-profiles"] = {
        "task": "apps.tasks.tasks.sync_ldap_profiles",
        "schedule": _cron(settings.EVILFLOWERS_LDAP_SYNC_SCHEDULE),
    }
# IP-018: fail extraction runs no service has reported to for EVILFLOWERS_EXTRACTION_STALE_HOURS.
if settings.EVILFLOWERS_EXTRACTION_ENABLED and settings.EVILFLOWERS_EXTRACTION_STALE_HOURS:
    app.conf.beat_schedule["extraction-fail-stale-runs"] = {
        "task": "apps.tasks.tasks.fail_stale_extraction_runs",
        "schedule": crontab(minute=41),
    }
# IP-031: the reconciler's tick (blocked jobs re-checked, retries, cancels); runs without events become
# `lost`; finished runs leave after their retention; providers' domain state is re-read daily.
app.conf.beat_schedule["jobs-reconcile"] = {
    "task": "apps.tasks.tasks.reconcile_jobs",
    "schedule": crontab(minute="*"),
}
app.conf.beat_schedule["jobs-sync"] = {
    "task": "apps.tasks.tasks.sync_jobs",
    "schedule": crontab(hour=4, minute=40),
}
app.conf.beat_schedule["jobs-sweep-lost"] = {
    "task": "apps.tasks.tasks.sweep_jobs",
    "schedule": crontab(minute="*/10"),
}
app.conf.beat_schedule["jobs-prune"] = {
    "task": "apps.tasks.tasks.prune_jobs",
    "schedule": crontab(hour=4, minute=10),
}
# #154: off-peak model work (off unless scheduled) and the nightly graph rebuild.
if settings.EVILFLOWERS_ASSISTANT_ENABLED and settings.EVILFLOWERS_ASSISTANT_SUMMARISE_SCHEDULE:
    app.conf.beat_schedule["assistant-summarise-sweep"] = {
        "task": "apps.assistant.tasks.summarise_sweep",
        "schedule": _cron(settings.EVILFLOWERS_ASSISTANT_SUMMARISE_SCHEDULE),
    }
if settings.EVILFLOWERS_ASSISTANT_ENABLED and settings.EVILFLOWERS_ASSISTANT_DESCRIBE_FIGURES_SCHEDULE:
    app.conf.beat_schedule["assistant-describe-figures"] = {
        "task": "apps.assistant.tasks.describe_figures_sweep",
        "schedule": _cron(settings.EVILFLOWERS_ASSISTANT_DESCRIBE_FIGURES_SCHEDULE),
    }
# #155: post-processing of every active run catches up by itself; similarity means refreshed weekly.
if settings.EVILFLOWERS_POST_PROCESS_SWEEP_SCHEDULE:
    app.conf.beat_schedule["extraction-post-process-sweep"] = {
        "task": "apps.extraction.tasks.post_process_sweep",
        "schedule": _cron(settings.EVILFLOWERS_POST_PROCESS_SWEEP_SCHEDULE),
    }
if settings.EVILFLOWERS_SIMILARITY_REBUILD_SCHEDULE:
    app.conf.beat_schedule["extraction-similarity-rebuild"] = {
        "task": "apps.extraction.tasks.similarity_rebuild",
        "schedule": _cron(settings.EVILFLOWERS_SIMILARITY_REBUILD_SCHEDULE),
    }
# #190, #191: the decision model types similar sections and suggests subjects, before the graph rebuild.
if settings.EVILFLOWERS_DECIDER_URL and settings.EVILFLOWERS_DECIDER_EDGE_SCHEDULE:
    app.conf.beat_schedule["decider-edge-sweep"] = {
        "task": "apps.decider.tasks.edge_sweep",
        "schedule": _cron(settings.EVILFLOWERS_DECIDER_EDGE_SCHEDULE),
    }
if settings.EVILFLOWERS_DECIDER_URL and settings.EVILFLOWERS_DECIDER_SUBJECT_SCHEDULE:
    app.conf.beat_schedule["decider-subject-sweep"] = {
        "task": "apps.decider.tasks.subject_sweep",
        "schedule": _cron(settings.EVILFLOWERS_DECIDER_SUBJECT_SCHEDULE),
    }
if settings.EVILFLOWERS_GRAPH_REBUILD_SCHEDULE:
    app.conf.beat_schedule["graph-rebuild"] = {
        "task": "apps.graph.tasks.rebuild_graph",
        "schedule": _cron(settings.EVILFLOWERS_GRAPH_REBUILD_SCHEDULE),
    }
# IP-033: oai_records re-projected nightly (QuerySet.update(), bulk changes, crosswalk upgrades).
if settings.EVILFLOWERS_OAI_ENABLED and settings.EVILFLOWERS_OAI_RECONCILE_SCHEDULE:
    app.conf.beat_schedule["oai-reconcile"] = {
        "task": "apps.oai.tasks.reconcile",
        "schedule": _cron(settings.EVILFLOWERS_OAI_RECONCILE_SCHEDULE),
    }
# IP-034: due sources get a `harvest.run` job. A two-way source's reconcile (changes and deletions no webhook
# reports, e.g. Dataverse deaccessions) is that source's own schedule, not a beat entry of its connector.
if settings.EVILFLOWERS_HARVEST_TICK_SCHEDULE:
    app.conf.beat_schedule["harvest-tick"] = {
        "task": "apps.harvest.tasks.tick",
        "schedule": _cron(settings.EVILFLOWERS_HARVEST_TICK_SCHEDULE),
    }
# IP-016: drop personal activity history older than the retention window.
app.conf.beat_schedule["core-prune-user-activity"] = {
    "task": "apps.tasks.tasks.prune_user_activity",
    "schedule": crontab(hour=3, minute=30),
}
# #175: the content disclosure log keeps each policy's `log.retention_days`.
app.conf.beat_schedule["content-prune-disclosures"] = {
    "task": "apps.tasks.tasks.prune_content_disclosures",
    "schedule": crontab(hour=3, minute=45),
}
# #194: read events (no personal data) outlive the activity history; kept at least the 400-day statistics span.
app.conf.beat_schedule["core-prune-read-events"] = {
    "task": "apps.tasks.tasks.prune_read_events",
    "schedule": crontab(hour=3, minute=40),
}
