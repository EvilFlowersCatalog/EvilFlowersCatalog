from django.db import models
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class JobProtocol(BaseModel):
    class Meta:
        app_label = "tasks"
        db_table = "job_protocols"
        default_permissions = ("view", "add")
        verbose_name = _("Job protocol")
        verbose_name_plural = _("Job protocols")

    class JobStatus(models.TextChoices):
        CREATED = "created", _("created")
        RECEIVED = "received", _("received")
        IN_PROGRESS = "in-progress", _("in-progress")
        FAILED = "failed", _("failed")
        REVOKED = "revoked", _("revoked")
        SUCCESS = "success", _("success")

    parent = models.ForeignKey("JobProtocol", on_delete=models.CASCADE, null=True)
    name = models.CharField(max_length=200)
    worker = models.CharField(max_length=50, null=True)
    status = models.CharField(
        max_length=20,
        choices=JobStatus.choices,
        default=JobStatus.CREATED,
    )
    job_args = JSONField(default=list)
    job_kwargs = JSONField(default=dict)
    result = JSONField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
