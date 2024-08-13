import django_rq
from django.db import models
from django.db.models import JSONField
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class JobProtocol(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'job_protocols'
        default_permissions = ('view', 'add')
        verbose_name = _('Job protocol')
        verbose_name_plural = _('Job protocols')

    class JobStatus(models.TextChoices):
        CREATED = "created", _("created")
        PENDING = "pending", _("pending")
        IN_PROGRESS = "in-progress", _("in-progress")
        FAILED = "failed", _("failed")
        DONE = "done", _("done")

    name = models.CharField(max_length=200)
    worker = models.CharField(max_length=50, null=True)
    status = models.CharField(
        max_length=20,
        choices=JobStatus.choices,
        default=JobStatus.CREATED,
    )
    job_args = JSONField(default=list)
    job_kwargs = JSONField(default=dict)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)


@receiver(post_save, sender=JobProtocol)
def add_to_queue(sender, instance: JobProtocol, created: bool, **kwargs):
    if created:
        queue = django_rq.get_queue('default')
        queue.enqueue(
            instance.name,
            job_id=str(instance.id),
            args=instance.job_args,
            kwargs=instance.job_kwargs
        )
