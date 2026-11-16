"""OAI job kinds (IP-031): the per-edit sync and the nightly reconcile of `oai_records` (IP-033)."""

from datetime import timedelta

from apps.oai.tasks import RECONCILE_TIME_LIMIT
from apps.tasks.contract import maintenance, run_only

run_only(
    "oai.sync",
    "apps.oai.tasks.sync_entries",
    "OAI-PMH record sync",
    "Re-project edited entries into oai_records; the datestamp moves only when the record changed (IP-033).",
)
maintenance(
    "maintenance.oai_reconcile",
    "apps.oai.tasks.reconcile",
    "Reconcile OAI-PMH records",
    "Re-project every harvestable entry into oai_records and tombstone records whose entry is gone or no longer "
    "exposed; catches QuerySet.update(), bulk changes and crosswalk upgrades (IP-033).",
    time_limit=timedelta(seconds=RECONCILE_TIME_LIMIT),
)
