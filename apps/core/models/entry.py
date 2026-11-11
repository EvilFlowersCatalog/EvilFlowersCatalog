import hashlib
from typing import Optional, TypedDict, Literal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.postgres.fields import HStoreField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from pgvector.django import HalfVectorField, HnswIndex
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


#: multilingual-e5-large-instruct (IP-021); a model of another size needs a migration.
ENTRY_EMBEDDING_DIMENSIONS = 1024

#: Columns maintained in SQL by `apps.core.search` (index refresh, embedding task), never by forms.
SEARCH_COLUMNS = (
    "search_vector",
    "search_authors",
    "search_subjects",
    "search_summary",
    "embedding",
    "embedding_model",
)


#: IP-023: the rating aggregate, written by `apps.core.services.reviews` only.
RATING_COLUMNS = ("rating_count", "rating_sum")

# (file field, hash column) pairs versioning the cover URLs (#128).
COVER_HASH_FIELDS = (("image", "image_hash"), ("thumbnail", "thumbnail_hash"))


def cover_bytes_hash(data: bytes) -> str:
    """The version of a cover whose bytes the writer holds (#128); same value as `cover_content_hash`."""
    return hashlib.sha256(data).hexdigest()[:16]


def cover_content_hash(file) -> Optional[str]:
    """First 16 hex chars of the SHA-256 of a stored cover; None without a file or when storage lacks it."""
    if not file or not file.name:
        return None
    digest = hashlib.sha256()
    try:
        with file.storage.open(file.name, "rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
    except Exception:  # noqa: BLE001 — minio raises its own S3Error; no hash just means an unversioned URL
        return None
    return digest.hexdigest()[:16]


class EntryManager(models.Manager):
    def get_queryset(self):
        # The search columns are only read in SQL. Deferring them keeps them out of every entry
        # query, and `save()` on a deferred instance never writes back a stale value.
        return super().get_queryset().defer(*SEARCH_COLUMNS)


class Entry(BaseModel):
    objects = EntryManager()

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
            # IP-020: full-text search
            GinIndex(fields=["search_vector"], name="entries_search_vector_gin"),
            # IP-021: semantic search (the BM25 index is created by migration, see apps.core.search.ddl)
            HnswIndex(name="entries_embedding_hnsw", fields=["embedding"], opclasses=["halfvec_cosine_ops"]),
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
    # Thumbnail MIME can differ from image_mime: covers are re-encoded to JPEG
    # for the preview grid. NULL on legacy rows — serving falls back to image_mime.
    thumbnail_mime = models.CharField(max_length=100, null=True)
    # #128: content hashes (first 16 hex chars of SHA-256) of the stored cover and thumbnail.
    # They version the URLs (`?v=`) and are the strong ETag. `save()` recomputes one whenever
    # its file name changes, so reading a book or editing metadata never rolls them. NULL means
    # not computed yet (`backfill_cover_hashes`): such a cover gets an unversioned URL.
    image_hash = models.CharField(max_length=16, null=True, editable=False)
    thumbnail_hash = models.CharField(max_length=16, null=True, editable=False)
    popularity = models.PositiveBigIntegerField(default=0, null=False)
    config = models.JSONField(null=False, default=default_entry_config)
    citation = models.TextField(null=True)
    touched_at = models.DateTimeField(null=True, auto_now=True)

    # Advanced entry details (issue #50; ratings/reviews in #57, IP-023)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    table_of_contents = models.JSONField(null=True, blank=True)
    related_entries = models.ManyToManyField("self", symmetrical=False, related_name="related_to", blank=True)

    # IP-023: published reviews of this entry, maintained by `ReviewService` (never by forms or `save()`).
    rating_count = models.PositiveIntegerField(default=0, editable=False)
    rating_sum = models.PositiveIntegerField(default=0, editable=False)

    # IP-020: maintained by `apps.core.search.index.refresh_search_vectors` (signals), never by forms.
    # `search_config` is the text search configuration (regconfig name) the vector was built with.
    search_config = models.CharField(max_length=63, null=True, editable=False)
    search_vector = SearchVectorField(null=True, editable=False)
    # IP-021: the related text, denormalised for the BM25 index (pg_search indexes columns of one
    # table only): author names, category terms + labels in every language, summary without tags.
    search_authors = models.TextField(null=True, editable=False)
    search_subjects = models.TextField(null=True, editable=False)
    search_summary = models.TextField(null=True, editable=False)
    # IP-021: semantic vector of title + authors + categories + summary, and the model (`id@revision`)
    # that produced it; vectors of two models are never compared.
    embedding = HalfVectorField(dimensions=ENTRY_EMBEDDING_DIMENSIONS, null=True, editable=False)
    embedding_model = models.CharField(max_length=255, null=True, editable=False)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # The file names the hashes were computed for; `save()` rehashes a file whose name differs.
        instance._stored_cover_names = {
            field: instance.__dict__[field] for field, _hash in COVER_HASH_FIELDS if field in instance.__dict__
        }
        return instance

    def _refresh_cover_hashes(self, update_fields: Optional[list[str]]) -> Optional[list[str]]:
        """Recompute `image_hash` / `thumbnail_hash` for a file whose name changed since loading.

        Storage never overwrites a name (a new upload gets a new one), so a new name is the only
        way a cover's body changes. Returns `update_fields` with the rehashed columns added.
        """
        stored = getattr(self, "_stored_cover_names", {})
        deferred = self.get_deferred_fields()
        for field, hash_field in COVER_HASH_FIELDS:
            if field in deferred or (update_fields is not None and field not in update_fields):
                continue
            file = getattr(self, field)
            name = file.name if file else None
            if not self._state.adding and field in stored and (stored[field] or None) == (name or None):
                continue
            setattr(self, hash_field, cover_content_hash(file))
            if update_fields is not None and hash_field not in update_fields:
                update_fields = [*update_fields, hash_field]
        return update_fields

    def save(self, *args, **kwargs):
        # The rating aggregate is written only by `ReviewService` with a targeted UPDATE. A full save of an
        # instance loaded before a review changed would put the old numbers back, so it leaves them out.
        if not self._state.adding and kwargs.get("update_fields") is None and not kwargs.get("force_insert"):
            skipped = set(RATING_COLUMNS) | self.get_deferred_fields()
            kwargs["update_fields"] = [
                field.name
                for field in self._meta.concrete_fields
                if not field.primary_key and field.name not in skipped and field.attname not in skipped
            ]
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = self._refresh_cover_hashes(list(kwargs["update_fields"]))
        else:
            self._refresh_cover_hashes(None)
        super().save(*args, **kwargs)
        written = kwargs.get("update_fields")
        deferred = self.get_deferred_fields()
        stored = getattr(self, "_stored_cover_names", {})
        for field, _hash in COVER_HASH_FIELDS:
            if field not in deferred and (written is None or field in written):
                stored[field] = getattr(self, field).name or None
        self._stored_cover_names = stored

    @property
    def rating_average(self) -> Optional[float]:
        if not self.rating_count:
            return None
        return round(self.rating_sum / self.rating_count, 2)

    @property
    def rating(self) -> Optional[dict]:
        """The `rating` block of the API (IP-023); None when reviews are switched off."""
        if not settings.EVILFLOWERS_REVIEWS_ENABLED:
            return None
        return {"average": self.rating_average, "count": self.rating_count}

    @property
    def first_author_name(self) -> str:
        first = self.authors.first()
        if first is None:
            return ""
        return (
            getattr(first, "full_name", None)
            or f"{getattr(first, 'name', '')} {getattr(first, 'surname', '')}".strip()
        )

    @staticmethod
    def _versioned(path: str, version: Optional[str]) -> str:
        # #128: `?v=<content hash>` is answered `immutable`; without a hash the URL stays unversioned.
        return f"{path}?{urlencode({'v': version})}" if version else path

    @property
    def image_url(self) -> Optional[str]:
        if not self.image:
            return None
        return self._versioned(reverse("files:cover-download", kwargs={"entry_id": self.pk}), self.image_hash)

    @property
    def thumbnail_url(self) -> Optional[str]:
        # Checks the thumbnail itself: placeholders exist without a cover, and a cover without a
        # generated thumbnail used to get a URL that 404s (#128).
        if not self.thumbnail:
            return None
        return self._versioned(reverse("files:thumbnail-download", kwargs={"entry_id": self.pk}), self.thumbnail_hash)

    def read_config(self, config_name: str):
        current = default_entry_config() | self.config
        return current.get(config_name)


@receiver(post_save, sender=Entry)
def touch_parents(sender, instance: Entry, **kwargs):
    # Bump only the `touched_at` column with a targeted UPDATE instead of a
    # full `catalog.save()` — the latter rewrites every column, bumps the
    # catalog's own `updated_at`, and re-fires Catalog's save signals on every
    # entry write (heavy under bulk imports / batch edits).
    now = timezone.now()
    Catalog.objects.filter(pk=instance.catalog_id).update(touched_at=now)
    instance.feeds.update(touched_at=now)


# IP-008 Phase 3 D2: the post-save encryption trigger lived here, which
# forced `apps.core` to import `apps.readium.services` — the dependency
# direction was wrong. The receiver now lives in `apps/readium/signals.py`
# and subscribes to `Entry.post_save` from the readium side. Behavior is
# unchanged; only the import direction flips so `apps.core` stays free of
# readium concepts.


__all__ = ["Entry", "default_entry_config", "ENTRY_EMBEDDING_DIMENSIONS", "SEARCH_COLUMNS"]
