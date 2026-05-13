from typing import Optional, TypedDict, Literal

from django.conf import settings
from django.contrib.postgres.fields import HStoreField
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from partial_date import PartialDateField

from apps.core.models.author import Author
from apps.core.models.entry_author import EntryAuthor
from apps.core.models.language import Language
from apps.core.models.category import Category
from apps.core.models.user import User
from apps.core.models.catalog import Catalog
from apps.core.models.base import BaseModel
from apps.core.validators import AvailableKeysValidator
from apps.files.storage import get_storage


class EntryConfig(TypedDict):
    evilflowers_ocr_enabled: bool
    evilflowers_ocr_rewrite: bool
    evilflowers_annotations_create: bool
    evilflowers_viewer_print: bool
    evilflowers_render_type: Literal["page", "document"]
    evilflowers_share_enabled: bool
    evilflowers_metadata_fetch: bool
    evilflowers_ip_block: bool
    readium_enabled: bool
    readium_amount: int


def default_entry_config() -> EntryConfig:
    return EntryConfig(
        evilflowers_ocr_enabled=False,
        evilflowers_ocr_rewrite=False,
        evilflowers_annotations_create=True,
        evilflowers_viewer_print=True,
        evilflowers_share_enabled=True,
        evilflowers_render_type="document",
        evilflowers_metadata_fetch=False,
        evilflowers_ip_block=False,
        readium_enabled=False,
        readium_amount=1,
    )


class Entry(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "entries"
        default_permissions = ()
        verbose_name = _("Entry")
        verbose_name_plural = _("Entries")
        indexes = [
            # Original index
            models.Index(fields=["catalog_id", "-popularity"]),
            # New performance indexes
            models.Index(fields=["catalog_id", "language_id"]),
            models.Index(fields=["catalog_id", "published_at"]),
            models.Index(fields=["catalog_id", "-created_at"]),
            models.Index(fields=["title"]),
            models.Index(fields=["publisher"]),
            models.Index(fields=["creator_id"]),
            # Composite indexes for common filter combinations
            models.Index(fields=["catalog_id", "language_id", "-popularity"]),
            models.Index(fields=["catalog_id", "-created_at", "-popularity"]),
            models.Index(fields=["catalog_id", "published_at", "-popularity"]),
            # Text search optimization
            models.Index(fields=["title", "catalog_id"]),
            models.Index(fields=["summary", "catalog_id"]),
        ]

    def _upload_to_path(self, filename):
        return f"catalogs/{self.catalog.url_name}/{self.pk}/{filename}"

    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name="entries")
    authors = models.ManyToManyField(Author, related_name="entries", through=EntryAuthor)
    language = models.ForeignKey(Language, on_delete=models.CASCADE, related_name="entries", null=True)
    identifiers = HStoreField(
        null=True,
        validators=[AvailableKeysValidator(keys=settings.EVILFLOWERS_IDENTIFIERS)],
    )
    title = models.CharField(max_length=255)
    published_at = PartialDateField(null=True)
    publisher = models.CharField(max_length=255, null=True)
    summary = models.TextField(null=True)
    content = models.TextField(null=True)
    categories = models.ManyToManyField(
        Category,
        related_name="entries",
        db_table="entry_categories",
        verbose_name=_("Category"),
    )
    image = models.ImageField(upload_to=_upload_to_path, null=True, max_length=255, storage=get_storage)
    image_mime = models.CharField(max_length=100, null=True)
    thumbnail = models.ImageField(upload_to=_upload_to_path, null=True, max_length=255, storage=get_storage)
    popularity = models.PositiveBigIntegerField(default=0, null=False)
    config = models.JSONField(null=False, default=default_entry_config)
    citation = models.TextField(null=True)
    touched_at = models.DateTimeField(null=True, auto_now=True)

    # Advanced entry details (issue #50; ratings/reviews deferred to #57)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    table_of_contents = models.JSONField(null=True, blank=True)
    related_entries = models.ManyToManyField(
        "self", symmetrical=False, related_name="related_to", blank=True
    )

    @property
    def image_url(self) -> Optional[str]:
        if not self.image:
            return None
        return reverse("files:cover-download", kwargs={"entry_id": self.pk})

    @property
    def thumbnail_url(self) -> Optional[str]:
        if not self.image:
            return None
        return reverse("files:thumbnail-download", kwargs={"entry_id": self.pk})

    def read_config(self, config_name: str):
        current = default_entry_config() | self.config
        return current.get(config_name)


@receiver(post_save, sender=Entry)
def touch_parents(sender, instance: Entry, **kwargs):
    instance.catalog.touched_at = timezone.now()
    instance.catalog.save()
    instance.feeds.update(touched_at=timezone.now())


@receiver(post_save, sender=Entry)
def trigger_readium_encryption(sender, instance: Entry, **kwargs):
    """Trigger LCP encryption when readium_enabled is set on an entry with existing acquisitions."""
    if not instance.read_config("readium_enabled"):
        return

    import logging

    from apps.readium.services import ContentEncryptionService

    logger = logging.getLogger(__name__)

    for acquisition in instance.acquisitions.filter(mime__in=["application/epub+zip", "application/pdf"]):
        if not acquisition.content or hasattr(acquisition, "encrypted_content"):
            continue

        try:
            ContentEncryptionService.encrypt_acquisition(acquisition)
            logger.info(f"Triggered LCP encryption for acquisition {acquisition.pk}")
        except ValueError as e:
            logger.warning(f"Failed to trigger encryption for acquisition {acquisition.pk}: {e}")


__all__ = ["Entry", "default_entry_config"]
