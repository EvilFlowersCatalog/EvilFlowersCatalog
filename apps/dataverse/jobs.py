"""Dataverse job kind (IP-031): resuming a paused publication workflow.

Its arguments carry the Dataverse API token, which the ledger redacts; such a job is listed and can be
revoked, but never re-sent from the ledger (the token is not stored).
"""

from apps.tasks.kinds import JobKind, registry


@registry.register
class ResumeWorkflowKind(JobKind):
    name = "dataverse.resume_workflow"
    title = "Dataverse workflow resume"
    description = "Resume a Dataverse workflow invocation after a prepublish webhook."
    task = "apps.dataverse.tasks.resume_workflow"
    startable = False
    exclusive = False
    terminate_safe = True
