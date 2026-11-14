"""Maintenance job kinds (IP-031): the beat tasks of `apps.tasks`, startable by hand."""

from apps.tasks.kinds import SystemKind, registry


@registry.register
class BackupKind(SystemKind):
    name = "maintenance.backup"
    title = "Backup"
    description = "PostgreSQL dump and per-catalog archives to EVILFLOWERS_BACKUP_DESTINATION."
    task = "apps.tasks.tasks.backup"


@registry.register
class CleanupOrphansKind(SystemKind):
    name = "maintenance.cleanup_orphans"
    title = "Delete orphaned storage"
    description = "Delete storage directories no row owns, older than EVILFLOWERS_ORPHAN_CLEANUP_MIN_AGE_HOURS."
    task = "apps.tasks.tasks.cleanup_orphans"


@registry.register
class CleanupPersonalisedCopiesKind(SystemKind):
    name = "maintenance.cleanup_personalised_copies"
    title = "Delete idle personalised copies"
    description = "Delete personalised copies no reader used for EVILFLOWERS_PERSONALISED_COPY_IDLE_HOURS."
    task = "apps.tasks.tasks.cleanup_personalised_copies"


@registry.register
class SyncLdapProfilesKind(SystemKind):
    name = "maintenance.sync_ldap_profiles"
    title = "Sync LDAP profiles"
    description = "Import e-mails and names of LDAP users who have not logged in since the mapping changed."
    task = "apps.tasks.tasks.sync_ldap_profiles"


@registry.register
class FailStaleExtractionRunsKind(SystemKind):
    name = "maintenance.fail_stale_extraction_runs"
    title = "Fail stale extraction runs"
    description = "Fail extraction runs no service reported to for EVILFLOWERS_EXTRACTION_STALE_HOURS."
    task = "apps.tasks.tasks.fail_stale_extraction_runs"


@registry.register
class PruneUserActivityKind(SystemKind):
    name = "maintenance.prune_user_activity"
    title = "Prune activity history"
    description = "Drop personal activity history older than the retention window (IP-016)."
    task = "apps.tasks.tasks.prune_user_activity"


@registry.register
class SweepJobsKind(SystemKind):
    name = "maintenance.sweep_jobs"
    title = "Sweep lost jobs"
    description = "Mark started jobs `lost` when no event arrived for their time limit (IP-031)."
    task = "apps.tasks.tasks.sweep_jobs"


@registry.register
class PruneJobsKind(SystemKind):
    name = "maintenance.prune_jobs"
    title = "Prune the job ledger"
    description = "Delete finished jobs past EVILFLOWERS_JOBS_RETENTION_DAYS / _FAILED_DAYS (IP-031)."
    task = "apps.tasks.tasks.prune_jobs"
