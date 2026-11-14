"""Dataverse job kind (IP-031): run-only. Its arguments carry the Dataverse API token, which the
ledger redacts; such a run is listed but never re-sent from the ledger."""

from apps.tasks.contract import run_only

run_only(
    "dataverse.resume_workflow",
    "apps.dataverse.tasks.resume_workflow",
    "Dataverse workflow resume",
    "Resume a Dataverse workflow invocation after a prepublish webhook.",
)
