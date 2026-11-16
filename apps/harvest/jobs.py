"""The `harvest.run` job kind (IP-031, IP-034): one job per harvest source, spec `{dry_run, full}`.

Started by `POST /api/v1/jobs` (administrators), the MCP `run_harvest` tool (catalog managers: it checks
`manage` on the target catalog and declares the job itself), and the beat `tick` for scheduled sources.
"""

from datetime import timedelta
from uuid import UUID

from django.utils.translation import gettext as _

from apps.core.models import Entry
from apps.harvest.models import HarvestSource
from apps.tasks.contract import (
    Blocked,
    JobDefinition,
    Ready,
    Refused,
    Scope,
    Spec,
    entry_scope,
    maintenance,
    registry,
    uuid_arg,
)

#: 20,000 records at a few per second; the task's own limits match (`apps.harvest.tasks.run`).
TIME_LIMIT = timedelta(hours=6)


class HarvestRunSpec(Spec):
    #: Match and report only: no entry, category or cursor is written.
    dry_run: bool = False
    #: Ignore the cursor and unchanged-record skipping; a full OAI harvest also drops links of records gone.
    full: bool = False


@registry.register
class HarvestRunDefinition(JobDefinition[HarvestSource, HarvestRunSpec]):
    kind = "harvest.run"
    title = "Metadata harvest"
    description = (
        "Harvest one source (OAI-PMH or an uploaded MARC file) into its target catalog: match records by exact "
        "identifiers, then link, enrich or create entries by the source's mode (IP-034). `dry_run` writes only "
        "dry-run records; `full` re-reads the whole source. The run's result holds the counters."
    )
    task = "apps.harvest.tasks.run"
    subject_model = HarvestSource
    spec_model = HarvestRunSpec
    time_limit = TIME_LIMIT

    def scope(self, source):
        return Scope(catalog_id=source.target_catalog_id)

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "source_id")

    def check(self, source, spec):
        from apps.harvest.adapters import registry as adapters

        adapter_type = adapters.get(source.adapter)
        if adapter_type is None:
            return Refused(_("Unknown harvest adapter '%(name)s'.") % {"name": source.adapter})
        if not adapter_type.runnable:
            return Refused(_("Sources of adapter '%(name)s' are not run by harvest jobs.") % {"name": source.adapter})
        if adapter_type.needs_base_url and not source.base_url:
            return Refused(_("The source has no base URL."))
        if source.mode == HarvestSource.Mode.SYNC and not adapter_type.can_write(source):
            return Refused(
                _("Source '%(slug)s' is in sync mode, but its connector cannot write.") % {"slug": source.slug}
            )
        if adapter_type.needs_upload and not source.upload_path:
            return Refused(_("No file has been uploaded for this source."))
        if not source.enabled and not spec.dry_run:
            # Pilot safety: a disabled source can be tried with a dry run, never written from.
            return Refused(_("The source is disabled; enable it, or start a dry run."))
        return Ready()

    def kwargs(self, dispatch):
        return {"source_id": str(dispatch.subject.pk), "dry_run": dispatch.spec.dry_run, "full": dispatch.spec.full}


class EntrySyncSpec(Spec):
    #: The `sync` source to push to or pull from.
    source: UUID


class _EntrySyncDefinition(JobDefinition[Entry, EntrySyncSpec]):
    """One job per entry; the spec names the source. A second `sync` source of the same catalog changes the spec,
    which starts a new generation after the running one (IP-031)."""

    subject_model = Entry
    spec_model = EntrySyncSpec
    max_attempts = 3

    def scope(self, entry):
        return entry_scope(entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "entry_id")

    def check(self, entry, spec):
        source = HarvestSource.objects.filter(pk=spec.source).first()
        if source is None:
            return Refused(_("No such harvest source."))
        if source.mode != HarvestSource.Mode.SYNC:
            return Refused(_("Source '%(slug)s' is not in sync mode.") % {"slug": source.slug})
        if source.target_catalog_id != entry.catalog_id:
            return Refused(_("The entry is not in the target catalog of source '%(slug)s'.") % {"slug": source.slug})
        if not source.enabled:
            return Blocked(_("Source '%(slug)s' is disabled.") % {"slug": source.slug})
        return self._check_source(entry, source)

    def _check_source(self, entry, source):
        return Ready()

    def kwargs(self, dispatch):
        return {"entry_id": str(dispatch.subject.pk), "source_id": str(dispatch.spec.source)}


@registry.register
class HarvestPushDefinition(_EntrySyncDefinition):
    kind = "harvest.push"
    title = "Push to a synced system"
    description = (
        "Merge an entry with its record in a two-way `sync` source and write the result there; an entry without a "
        "record is created remotely (IP-034 Part B). Declared on entry changes and by `harvest.run`."
    )
    task = "apps.harvest.tasks.push"

    def _check_source(self, entry, source):
        from apps.harvest.adapters import registry as connectors

        connector = connectors.get(source.adapter)
        if connector is None or not connector.can_write(source):
            return Blocked(_("The connector of source '%(slug)s' cannot write now.") % {"slug": source.slug})
        return Ready()


@registry.register
class HarvestPullDefinition(_EntrySyncDefinition):
    kind = "harvest.pull"
    title = "Pull from a synced system"
    description = "Fetch an entry's record from a two-way `sync` source and merge it into the entry (IP-034)."
    task = "apps.harvest.tasks.pull"

    def _check_source(self, entry, source):
        if not (entry.identifiers or {}).get(source.system_key):
            return Refused(_("The entry has no record in source '%(slug)s' yet.") % {"slug": source.slug})
        return Ready()


# The beat tick that declares `harvest.run` for every enabled source whose own schedule is due.
maintenance(
    "maintenance.harvest_tick",
    "apps.harvest.tasks.tick",
    "Harvest schedule tick",
    "Declare a harvest run for every enabled source whose cron schedule is due (IP-034).",
)


__all__ = [
    "EntrySyncSpec",
    "HarvestPullDefinition",
    "HarvestPushDefinition",
    "HarvestRunDefinition",
    "HarvestRunSpec",
    "TIME_LIMIT",
]
