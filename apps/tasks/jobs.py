"""Maintenance job kinds (IP-031): one job per beat task; applying it runs the task now."""

from apps.tasks.contract import maintenance

maintenance(
    "maintenance.backup",
    "apps.tasks.tasks.backup",
    "Backup",
    "PostgreSQL dump and per-catalog archives to EVILFLOWERS_BACKUP_DESTINATION.",
)
maintenance(
    "maintenance.cleanup_orphans",
    "apps.tasks.tasks.cleanup_orphans",
    "Delete orphaned storage",
    "Delete storage directories no row owns, older than EVILFLOWERS_ORPHAN_CLEANUP_MIN_AGE_HOURS.",
)
maintenance(
    "maintenance.cleanup_personalised_copies",
    "apps.tasks.tasks.cleanup_personalised_copies",
    "Delete idle personalised copies",
    "Delete personalised copies no reader used for a while (#70).",
)
maintenance(
    "maintenance.sync_ldap_profiles",
    "apps.tasks.tasks.sync_ldap_profiles",
    "Sync LDAP profiles",
    "Import e-mails and names of LDAP users who have not logged in since the mapping changed.",
)
maintenance(
    "maintenance.fail_stale_extraction_runs",
    "apps.tasks.tasks.fail_stale_extraction_runs",
    "Fail stale extraction runs",
    "Fail extraction runs no service reported to for EVILFLOWERS_EXTRACTION_STALE_HOURS.",
)
maintenance(
    "maintenance.prune_user_activity",
    "apps.tasks.tasks.prune_user_activity",
    "Prune activity history",
    "Drop personal activity history older than the retention window (IP-016).",
)
maintenance(
    "maintenance.sweep_jobs",
    "apps.tasks.tasks.sweep_jobs",
    "Sweep lost runs",
    "Mark runs `lost` when no event arrived for their time limit (IP-031).",
)
maintenance(
    "maintenance.prune_jobs",
    "apps.tasks.tasks.prune_jobs",
    "Prune job runs",
    "Delete finished runs past EVILFLOWERS_JOBS_RETENTION_DAYS / _FAILED_DAYS (IP-031).",
)
maintenance(
    "maintenance.sync_jobs",
    "apps.tasks.tasks.sync_jobs",
    "Sync jobs with the domain",
    "Create or refresh the jobs of every subject providers know (IP-031).",
)
