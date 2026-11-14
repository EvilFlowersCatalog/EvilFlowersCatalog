"""The job ledger (IP-031).

One `JobProtocol` row per Celery task id, whoever sent the task: `JobService` (a start, retry or
action), any other catalog code (`.delay()`, beat), or a service worker (its follow-up tasks, seen
through Celery events only). `JobEvent` keeps the events behind a row's state.

The table and model names predate IP-031 and are kept.
"""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class JobProtocol(BaseModel):
    class Meta:
        app_label = "tasks"
        db_table = "job_protocols"
        default_permissions = ("view", "add")
        permissions = (("report_job", _("Can report the outcome of a background job")),)
        verbose_name = _("Job protocol")
        verbose_name_plural = _("Job protocols")
        indexes = [
            models.Index(fields=["kind", "subject_type", "subject_id", "-created_at"], name="job_kind_subject_idx"),
            models.Index(fields=["status", "created_at"], name="job_status_created_idx"),
            models.Index(fields=["catalog", "kind", "status"], name="job_catalog_kind_status_idx"),
            models.Index(fields=["entry", "-created_at"], name="job_entry_created_idx"),
        ]

    class JobStatus(models.TextChoices):
        QUEUED = "queued", _("queued")
        RECEIVED = "received", _("received")
        STARTED = "started", _("started")
        RETRYING = "retrying", _("retrying")
        SUCCEEDED = "succeeded", _("succeeded")
        FAILED = "failed", _("failed")
        REVOKED = "revoked", _("revoked")
        REJECTED = "rejected", _("rejected")
        LOST = "lost", _("lost")

    #: A job in one of these has not finished; at most one per kind and subject when the kind is exclusive.
    ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RECEIVED, JobStatus.STARTED, JobStatus.RETRYING)
    TERMINAL_STATUSES = (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.REVOKED,
        JobStatus.REJECTED,
        JobStatus.LOST,
    )

    # Celery
    name = models.CharField(max_length=200)
    parent = models.ForeignKey(
        "JobProtocol", on_delete=models.DO_NOTHING, null=True, db_constraint=False, related_name="children"
    )
    queue = models.CharField(max_length=100, null=True)
    worker = models.CharField(max_length=255, null=True)
    status = models.CharField(max_length=20, choices=JobStatus.choices, default=JobStatus.QUEUED)
    job_args = JSONField(default=list)
    job_kwargs = JSONField(default=dict)
    #: `job_args`/`job_kwargs` are the exact (JSON) call, recorded by the catalog when it sent the task,
    #: not Celery's truncated `saferepr` from an event, and nothing was redacted: the task can be re-sent.
    args_exact = models.BooleanField(default=False)
    result = JSONField(null=True)
    error = models.TextField(null=True)
    traceback = models.TextField(null=True)
    runtime = models.FloatField(null=True)
    attempts = models.PositiveIntegerField(default=0)

    # Event times (the events' own timestamps)
    sent_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    #: Precedence guard: an event older than this is history, not state.
    last_event_at = models.DateTimeField(null=True)

    # IP-031: what the job is about
    kind = models.CharField(max_length=100, null=True, db_index=True)
    subject_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, related_name="+")
    subject_id = models.UUIDField(null=True)
    catalog = models.ForeignKey("core.Catalog", on_delete=models.SET_NULL, null=True, related_name="+")
    entry = models.ForeignKey("core.Entry", on_delete=models.SET_NULL, null=True, related_name="+")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    options = JSONField(default=dict)
    retry_of = models.ForeignKey(
        "JobProtocol", on_delete=models.DO_NOTHING, null=True, db_constraint=False, related_name="retries"
    )
    report = JSONField(null=True)
    reported_at = models.DateTimeField(null=True)
    revoke_requested_at = models.DateTimeField(null=True)
    trace_id = models.CharField(max_length=32, null=True)

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def stale(self) -> bool:
        from apps.tasks.services import is_stale

        return is_stale(self)

    @property
    def subject_label(self):
        """`app_label.model` of the subject, e.g. `core.acquisition`."""
        if self.subject_type_id is None:
            return None
        content_type = ContentType.objects.get_for_id(self.subject_type_id)
        return f"{content_type.app_label}.{content_type.model}"

    def __str__(self):
        return f"{self.kind or self.name} {self.pk} ({self.status})"


class JobEvent(BaseModel):
    """One Celery event (or a catalog-side change: queued, revoke requested, lost) of a job."""

    class Meta:
        app_label = "tasks"
        db_table = "job_events"
        default_permissions = ()
        ordering = ("timestamp", "created_at")
        indexes = [models.Index(fields=["job", "timestamp"], name="job_event_job_ts_idx")]

    job = models.ForeignKey(JobProtocol, on_delete=models.CASCADE, db_constraint=False, related_name="events")
    type = models.CharField(max_length=30)
    timestamp = models.DateTimeField()
    hostname = models.CharField(max_length=255, null=True)
    data = JSONField(default=dict)


__all__ = ["JobProtocol", "JobEvent"]
