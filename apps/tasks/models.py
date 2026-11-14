"""Persistence of the background-jobs domain (IP-031 rev. 2).

- `Job`: the declarative aggregate, one per kind and subject (one per kind for subjectless kinds):
  what a client wants (`spec`, `desired_state`, `generation`) and what is observed (`state`).
- `JobRun`: one Celery task execution, whoever sent it; facts only (table `job_protocols`, the
  pre-IP-031 ledger). Runs of run-only kinds (e-mails, embedding batches) have no job.
- `JobEvent`: a run's Celery events and a job's transitions.
"""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel
from apps.tasks.domain.values import DesiredState, JobState, RunStatus


def _choices(enum):
    return [(member.value, member.value) for member in enum]


class SubjectMixin(models.Model):
    """What a job or run is about, with its scope for filtering."""

    class Meta:
        abstract = True

    kind = models.CharField(max_length=100, null=True, db_index=True)
    subject_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, related_name="+")
    subject_id = models.UUIDField(null=True)
    catalog = models.ForeignKey("core.Catalog", on_delete=models.SET_NULL, null=True, related_name="+")
    entry = models.ForeignKey("core.Entry", on_delete=models.SET_NULL, null=True, related_name="+")

    @property
    def subject_label(self):
        """`app_label.model` of the subject, e.g. `core.acquisition`."""
        if self.subject_type_id is None:
            return None
        content_type = ContentType.objects.get_for_id(self.subject_type_id)
        return f"{content_type.app_label}.{content_type.model}"


class Job(SubjectMixin, BaseModel):
    class Meta:
        app_label = "tasks"
        db_table = "jobs"
        default_permissions = ("view",)
        verbose_name = _("Job")
        verbose_name_plural = _("Jobs")
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "subject_type", "subject_id"], name="job_kind_subject_unique", nulls_distinct=False
            )
        ]
        indexes = [
            models.Index(fields=["state", "kind"], name="job_state_kind_idx"),
            models.Index(fields=["catalog", "kind", "state"], name="job_catalog_kind_state_idx"),
            models.Index(fields=["entry"], name="job_entry_idx"),
        ]

    spec = JSONField(default=dict)
    desired_state = models.CharField(max_length=20, choices=_choices(DesiredState), default=DesiredState.COMPLETED)
    state = models.CharField(max_length=20, choices=_choices(JobState), default=JobState.PENDING)
    #: Why the job is blocked or failed (the check's or the run's reason).
    reason = models.TextField(null=True)
    #: What the provider observed in the domain (e.g. the LCP job of #148), for listing.
    detail = JSONField(default=dict)
    #: Bumped by every accepted apply; the reconciler acts while it is ahead of `observed_generation`.
    generation = models.PositiveIntegerField(default=0)
    observed_generation = models.PositiveIntegerField(default=0)
    #: Runs of the current generation.
    attempts = models.PositiveIntegerField(default=0)
    current_run = models.ForeignKey(
        "JobRun", on_delete=models.DO_NOTHING, null=True, db_constraint=False, related_name="+"
    )
    #: The job whose outcome declared this one (OCR → re-encryption).
    cause = models.ForeignKey(
        "self", on_delete=models.DO_NOTHING, null=True, db_constraint=False, related_name="consequences"
    )
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    last_transition_at = models.DateTimeField(null=True)

    @property
    def stale(self) -> bool:
        from apps.tasks.application.facade import is_stale

        return is_stale(self)

    def __str__(self):
        return f"{self.kind} {self.subject_id or ''} ({self.state})"


class JobRun(SubjectMixin, BaseModel):
    class Meta:
        app_label = "tasks"
        db_table = "job_protocols"
        default_permissions = ("view", "add")
        permissions = (("report_job", _("Can report the outcome of a background job")),)
        verbose_name = _("Job run")
        verbose_name_plural = _("Job runs")
        indexes = [
            models.Index(fields=["kind", "subject_type", "subject_id", "-created_at"], name="job_kind_subject_idx"),
            models.Index(fields=["status", "created_at"], name="job_status_created_idx"),
            models.Index(fields=["catalog", "kind", "status"], name="job_catalog_kind_status_idx"),
            models.Index(fields=["entry", "-created_at"], name="job_entry_created_idx"),
        ]

    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, db_constraint=False, related_name="runs")
    name = models.CharField(max_length=200)
    #: Celery's parent task (a service's follow-up tasks hang under the run that sent them).
    parent = models.ForeignKey(
        "self", on_delete=models.DO_NOTHING, null=True, db_constraint=False, related_name="children"
    )
    queue = models.CharField(max_length=100, null=True)
    worker = models.CharField(max_length=255, null=True)
    status = models.CharField(max_length=20, choices=_choices(RunStatus), default=RunStatus.QUEUED)
    job_args = JSONField(default=list)
    job_kwargs = JSONField(default=dict)
    #: The exact call (recorded when the catalog sent it), nothing redacted: it can be re-sent.
    args_exact = models.BooleanField(default=False)
    result = JSONField(null=True)
    error = models.TextField(null=True)
    traceback = models.TextField(null=True)
    runtime = models.FloatField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    report = JSONField(null=True)
    reported_at = models.DateTimeField(null=True)
    trace_id = models.CharField(max_length=32, null=True)

    sent_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    #: Precedence guard: an event older than this is history, not state.
    last_event_at = models.DateTimeField(null=True)

    @property
    def run_status(self) -> RunStatus:
        return RunStatus(self.status)

    @property
    def is_active(self) -> bool:
        return self.run_status.active

    def __str__(self):
        return f"{self.kind or self.name} {self.pk} ({self.status})"


class JobEvent(BaseModel):
    """A run's Celery event, or a job's transition (catalog side: applied, blocked, cancelled …)."""

    class Meta:
        app_label = "tasks"
        db_table = "job_events"
        default_permissions = ()
        ordering = ("timestamp", "created_at")
        indexes = [
            models.Index(fields=["run", "timestamp"], name="job_event_job_ts_idx"),
            models.Index(fields=["job_ref", "timestamp"], name="job_event_job_ref_ts_idx"),
        ]

    run = models.ForeignKey(JobRun, on_delete=models.CASCADE, null=True, db_constraint=False, related_name="events")
    job_ref = models.ForeignKey(Job, on_delete=models.CASCADE, null=True, db_constraint=False, related_name="events")
    type = models.CharField(max_length=40)
    timestamp = models.DateTimeField()
    hostname = models.CharField(max_length=255, null=True)
    data = JSONField(default=dict)


__all__ = ["Job", "JobEvent", "JobRun"]
