import base64
import hashlib
from typing import Optional

from celery import signature, chain, group
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models.entry import Entry
from apps.core.models.base import BaseModel
from apps.files.storage import get_storage

from apps.events.services import get_event_broker


class Acquisition(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "acquisitions"
        default_permissions = ()
        verbose_name = _("Acquisition")
        verbose_name_plural = _("Acquisitions")

    class AcquisitionType(models.TextChoices):
        ACQUISITION = "acquisition", _("acquisition")
        OPEN_ACCESS = "open-access", _("open-access")

        def __str__(self):
            if self == self.OPEN_ACCESS:
                return "http://opds-spec.org/acquisition/open-access"
            return "http://opds-spec.org/acquisition"

    class AcquisitionMIME(models.TextChoices):
        PDF = "application/pdf", _("PDF")
        EPUB = "application/epub+zip", _("EPUB")
        MOBI = "application/x-mobipocket-ebook", _("MOBI")
        READIUM_PACKAGE = "application/webpub+zip", _("READIUM PACKAGE")

    def upload_base_path(self):
        return f"catalogs/{self.entry.catalog.url_name}/{self.entry.pk}"

    def upload_to_path(self, filename):
        return f"{self.upload_base_path()}/{filename}"

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="acquisitions")
    relation = models.CharField(
        max_length=20,
        choices=AcquisitionType.choices,
        default=AcquisitionType.ACQUISITION,
    )
    mime = models.CharField(choices=AcquisitionMIME.choices, max_length=100)
    content = models.FileField(upload_to=upload_to_path, null=True, max_length=255, storage=get_storage)

    @property
    def url(self) -> Optional[str]:
        if not self.content:
            return None
        return reverse("files:acquisition-download", kwargs={"acquisition_id": self.pk})

    @property
    def base64(self) -> Optional[str]:
        if self.content is not None:
            encoded = base64.b64encode(self.content.read()).decode("ascii")
            return f"data:{self.mime};base64,{encoded}"
        return None

    @property
    def checksum(self) -> Optional[str]:
        if self.content is not None:
            checksum = hashlib.sha256()
            while block := self.content.read(4096):
                checksum.update(block)
            return checksum.hexdigest()
        return None


@receiver(post_save, sender=Acquisition)
def touch_entry(sender, instance: Acquisition, **kwargs):
    instance.entry.touched_at = timezone.now()
    instance.entry.save()


@receiver(post_save, sender=Acquisition)
def background_tasks(sender, instance: Acquisition, created: bool, **kwargs):
    dependent_tasks = []
    event_broker = get_event_broker()

    if created and instance.entry.language_id:
        # ocr_task = signature(
        #     "evilflowers_ocr_worker.ocr",
        #     args=[instance.content.name, instance.content.name, instance.entry.language.alpha3],
        #     immutable=True,
        # )
        event_broker.execute(
            "evilflowers_ocr_worker.ocr",
            {
                "args": [instance.content.name, instance.content.name, instance.entry.language.alpha3],
            },
        )
    else:
        ocr_task = None

    if instance.entry.config["readium_enabled"]:
        # dependent_tasks.append(
        #     signature(
        #         "evilflowers_lcpencrypt_worker.lcpencrypt",
        #         kwargs={
        #             "input_file": instance.content.name,
        #             "contentid": str(instance.pk),
        #             "storage": instance.upload_base_path(),  # FIXME: support for S3
        #             "filename": f"{instance.pk}.lcp.pdf",
        #         },
        #         immutable=True,
        #         # Queue have to be defined explicitly, settings.CELERY_TASK_ROUTES is ignored for some reason
        #         queue="evilflowers_lcpencrypt_worker",
        #     )
        # )
        event_broker.execute(
            "evilflowers_lcpencrypt_worker.lcpencrypt",
            kwargs={
                "input_file": instance.content.name,
                "contentid": str(instance.pk),
                "storage": instance.upload_base_path(),  # FIXME: support for S3
                "filename": f"{instance.pk}.lcp.pdf",
            },
            queue="evilflowers_lcpencrypt_worker",
        )

    # if ocr_task is not None:
    #     chain(ocr_task, group(dependent_tasks)).apply_async()
    # else:
    #     group(dependent_tasks).apply_async()


__all__ = ["Acquisition"]
