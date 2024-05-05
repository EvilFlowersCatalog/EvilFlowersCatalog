import base64
import logging
from typing import Optional, TypedDict, Literal

from django.conf import settings
from django.contrib.postgres.fields import HStoreField
from django.core.cache import cache
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


def default_entry_config() -> EntryConfig:
    return EntryConfig(
        evilflowers_ocr_enabled=False,
        evilflowers_ocr_rewrite=False,
        evilflowers_annotations_create=True,
        evilflowers_viewer_print=True,
        evilflowers_share_enabled=True,
        evilflowers_render_type="document",
        evilflowers_metadata_fetch=False,
    )


class Entry(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "entries"
        default_permissions = ()
        verbose_name = _("Entry")
        verbose_name_plural = _("Entries")
        indexes = [
            models.Index(fields=["catalog_id", "-popularity"]),
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

    @property
    def image_url(self) -> Optional[str]:
        if not self.image:
            return None
        return f"{settings.BASE_URL}{reverse('files:cover-download', kwargs={'entry_id': self.pk})}"

    @property
    def thumbnail_base64(self) -> Optional[str]:
        if not self.thumbnail:
            return None

        cache_key = f"entry:{self.id}:thumbnail:base64"
        cached = cache.get(cache_key)

        if cached:
            return cached

        try:
            encoded = base64.b64encode(self.thumbnail.read()).decode("ascii")
        except FileNotFoundError:
            logging.warning("Unable to find %s", self.thumbnail.path)
            return None

        result = f"data:{self.image_mime};base64,{encoded}"
        cache.set(cache_key, result, 60 * 60 * 24)  # TODO: settings
        return result

    def read_config(self, config_name: str):
        current = default_entry_config() | self.config
        return current.get(config_name)


@receiver(post_save, sender=Entry)
def touch_parents(sender, instance: Entry, **kwargs):
    instance.catalog.touched_at = timezone.now()
    instance.catalog.save()
    instance.feeds.update(touched_at=timezone.now())


__all__ = ["Entry"]
