import base64
import hashlib
from typing import Optional

from celery import signature, chain, group
from django.conf import settings
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
        BORROW = "borrow", _("borrow")
        # IP-008 Phase 4 D4: Dataverse files marked `restricted` are
        # imported with this relation so OPDS consumers can distinguish
        # them from open-access content.
        RESTRICTED_ACCESS = "restricted-access", _("restricted-access")

        def __str__(self):
            if self == self.OPEN_ACCESS:
                return "http://opds-spec.org/acquisition/open-access"
            elif self == self.BORROW:
                return "http://opds-spec.org/acquisition/borrow"
            elif self == self.RESTRICTED_ACCESS:
                return "http://opds-spec.org/acquisition"
            return "http://opds-spec.org/acquisition"

    class AcquisitionMIME(models.TextChoices):
        PDF = "application/pdf", _("PDF")
        EPUB = "application/epub+zip", _("EPUB")
        MOBI = "application/x-mobipocket-ebook", _("MOBI")
        READIUM_PACKAGE = "application/webpub+zip", _("READIUM PACKAGE")

    class StorageBackend(models.TextChoices):
        """IP-008 Phase 5: explicit storage mode.

        `LOCAL` — file lives in our `get_storage()` backend (FS or S3).
        `EXTERNAL_URL` — `file_url` points at an external host
        (Dataverse). Authentication checks must still apply before the
        URL is exposed to the requester.

        All dispatch goes through
        `apps/files/services.py::AcquisitionStorageService` so callers
        don't repeat the `if file_url else ...` branch.
        """

        LOCAL = "local", _("local")
        EXTERNAL_URL = "external_url", _("external_url")

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
    file_url = models.URLField(null=True, blank=True, max_length=2048)
    storage_backend = models.CharField(
        max_length=20,
        choices=StorageBackend.choices,
        default=StorageBackend.LOCAL,
    )
    # IP-008 Phase 5: lazy checksum cache. The SHA-256 is computed on
    # first demand (via the property below) and persisted here. The old
    # behaviour read the entire file on every detailed serialization.
    checksum_cached = models.CharField(max_length=64, null=True, blank=True)

    @property
    def url(self) -> Optional[str]:
        """Best-effort URL for the resource.

        Callers that need ACL enforcement should go through
        `apps.files.services.AcquisitionStorageService.url(...)`
        instead. This property is kept as a backward-compatible shim.
        """
        if self.storage_backend == self.StorageBackend.EXTERNAL_URL and self.file_url:
            return self.file_url
        if self.file_url:
            return self.file_url
        if not self.content:
            return None
        return reverse("files:acquisition-download", kwargs={"acquisition_id": self.pk})

    @property
    def base64(self) -> Optional[str]:
        """Base64-encoded content payload.

        IP-008 Phase 5 lazy-hashing note: this is expensive — it reads
        the full file each call. Detailed serializers no longer include
        it by default; clients opt in via `?include=content`.
        """
        if self.storage_backend != self.StorageBackend.LOCAL or self.content is None:
            return None
        encoded = base64.b64encode(self.content.read()).decode("ascii")
        return f"data:{self.mime};base64,{encoded}"

    @property
    def checksum(self) -> Optional[str]:
        """SHA-256 hex of the local file content (cached after first read)."""
        if self.storage_backend != self.StorageBackend.LOCAL or self.content is None:
            return None
        if self.checksum_cached:
            return self.checksum_cached
        digest = hashlib.sha256()
        while block := self.content.read(4096):
            digest.update(block)
        hexdigest = digest.hexdigest()
        # Persist for next time. `update_fields` keeps the write
        # narrow and avoids firing post_save side effects on unrelated
        # columns.
        Acquisition.objects.filter(pk=self.pk).update(checksum_cached=hexdigest)
        self.checksum_cached = hexdigest
        return hexdigest


@receiver(post_save, sender=Acquisition)
def touch_entry(sender, instance: Acquisition, **kwargs):
    instance.entry.touched_at = timezone.now()
    instance.entry.save()


@receiver(post_save, sender=Acquisition)
def background_tasks(sender, instance: Acquisition, created: bool, **kwargs):
    """
    Trigger background tasks (OCR, Readium encryption) after an acquisition is saved.

    For Dataverse / external acquisitions where `content` is empty and only `file_url` is set,
    we skip background processing because there is no local file to work with.
    We also skip if the event broker is not configured (development setups).
    """

    # Skip when there is no stored file (e.g., Dataverse URL-only acquisitions)
    if not instance.content:
        return

    # Skip when event broker executor is not configured
    if not getattr(settings, "EVILFLOWERS_EVENT_BROKER_EXECUTOR", None):
        return

    dependent_tasks = []
    event_broker = get_event_broker()

    # OCR task for new acquisitions with language set
    if created and instance.entry.language_id:
        event_broker.execute(
            "evilflowers_ocr_worker.ocr",
            {
                "args": [instance.content.name, instance.content.name, instance.entry.language.alpha3],
            },
        )


__all__ = ["Acquisition"]
