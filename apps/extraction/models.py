"""Extraction output of a PDF acquisition (IP-018).

The catalog owns this schema and nothing else of extraction: the
`evilflowers-*-service` containers do the work and post their results to the
ingestion endpoints (`apps.extraction.views`). Positions are against the
original file: 1-based page, boxes as fractions of the page, origin top-left.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _
from pgvector.django import HnswIndex, VectorField

from apps.core.models import Acquisition
from apps.core.models.base import BaseModel

#: Dimension of `EMBEDDING_MODEL` in text-service. Snowflake Arctic Embed
#: M v2.0 (the default) and stsb-xlm-r-multilingual are 768; a model of another
#: dimension needs a migration of this column and a pipeline version bump.
EMBEDDING_DIMENSIONS = 768


class ExtractionRun(BaseModel):
    """One extraction of one acquisition by one pipeline version.

    The analyzer declares the jobs of the run when it starts it; each service
    reports its job done or failed. When every job has reported, the run becomes
    the acquisition's active one in one transaction and the previous run is
    deleted together with its files.
    """

    class Meta:
        app_label = "extraction"
        db_table = "extraction_run"
        default_permissions = ()
        permissions = (("ingest_extraction", _("Can report extraction results")),)
        constraints = [
            models.UniqueConstraint(fields=["acquisition", "pipeline_version"], name="extraction_run_version"),
        ]
        verbose_name = _("Extraction run")
        verbose_name_plural = _("Extraction runs")

    class Status(models.TextChoices):
        QUEUED = "queued", _("queued")
        RUNNING = "running", _("running")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    class JobStatus(models.TextChoices):
        PENDING = "pending", _("pending")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    acquisition = models.ForeignKey(Acquisition, on_delete=models.CASCADE, related_name="extraction_runs")
    pipeline_version = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    is_active = models.BooleanField(default=False)
    page_count = models.PositiveIntegerField(null=True)
    #: `[[width, height], ...]` in PDF points, one per page of the original file.
    page_sizes = models.JSONField(null=True)
    #: `{job: pending | done | failed}`, declared by the analyzer when it starts the run.
    jobs = models.JSONField(default=dict)
    #: Image tags and digests, model names and versions that produced the run.
    provenance = models.JSONField(default=dict)
    error = models.TextField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    @property
    def storage_dir(self) -> str:
        """Where the services write this run's crops, relative to the storage root."""
        return storage_dir(self.acquisition, self.pipeline_version)


def storage_dir(acquisition: Acquisition, pipeline_version: int) -> str:
    return f"{acquisition.upload_base_path()}/extraction/{acquisition.pk}/v{pipeline_version}"


class Region(BaseModel):
    """A figure, table or equation on a page of the original file."""

    class Meta:
        app_label = "extraction"
        db_table = "extraction_region"
        default_permissions = ()
        ordering = ["page", "seq"]
        indexes = [models.Index(fields=["run", "page"])]
        verbose_name = _("Region")
        verbose_name_plural = _("Regions")

    class Kind(models.TextChoices):
        FIGURE = "figure", _("figure")
        TABLE = "table", _("table")
        EQUATION = "equation", _("equation")

    run = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="regions")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    page = models.PositiveIntegerField()
    #: Order on the page.
    seq = models.PositiveIntegerField()
    #: `{x0, y0, x1, y1}` page fractions, origin top-left.
    bbox = models.JSONField()
    #: Storage name of the crop, under the run's storage dir.
    image_path = models.CharField(max_length=255, null=True)
    #: What clients read: the printed caption, else the model's description.
    caption = models.TextField(null=True)
    #: The caption printed in the document ("Figure 2.1: ..."), verbatim (IP-018 Phase 8).
    printed_caption = models.TextField(null=True)
    #: A multimodal model's description of a figure, when image-service captions are on.
    description = models.TextField(null=True)
    category = models.CharField(max_length=50, null=True)
    markdown = models.TextField(null=True)
    latex = models.TextField(null=True)
    provenance = models.JSONField(default=dict)


class Paragraph(BaseModel):
    """A paragraph of the original file, the unit that is embedded (IP-018 Phase 2b)."""

    class Meta:
        app_label = "extraction"
        db_table = "extraction_paragraph"
        default_permissions = ()
        ordering = ["seq"]
        constraints = [models.UniqueConstraint(fields=["run", "seq"], name="extraction_paragraph_seq")]
        indexes = [
            HnswIndex(
                name="extraction_paragraph_embedding",
                fields=["embedding"],
                opclasses=["vector_cosine_ops"],
            ),
        ]
        verbose_name = _("Paragraph")
        verbose_name_plural = _("Paragraphs")

    run = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="paragraphs")
    page = models.PositiveIntegerField()
    #: Reading order in the document.
    seq = models.PositiveIntegerField()
    #: Outline heading the paragraph falls under.
    section = models.TextField(null=True)
    bbox = models.JSONField()
    text = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True)


class Sentence(BaseModel):
    """A sentence of a paragraph, the unit that is highlighted."""

    class Meta:
        app_label = "extraction"
        db_table = "extraction_sentence"
        default_permissions = ()
        ordering = ["paragraph", "seq"]
        verbose_name = _("Sentence")
        verbose_name_plural = _("Sentences")

    paragraph = models.ForeignKey(Paragraph, on_delete=models.CASCADE, related_name="sentences")
    seq = models.PositiveIntegerField()
    #: Character offsets into `paragraph.text`.
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    #: A sentence may continue on the next page; its quads are on this page.
    page = models.PositiveIntegerField()
    #: `[{x0, y0, x1, y1}, ...]` page fractions.
    quads = models.JSONField(default=list)

    @property
    def text(self) -> str:
        return self.paragraph.text[self.start : self.end]


class ParagraphLink(BaseModel):
    class Meta:
        app_label = "extraction"
        db_table = "extraction_paragraph_link"
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["source", "target", "kind"], name="extraction_paragraph_link_unique"),
        ]
        verbose_name = _("Paragraph link")
        verbose_name_plural = _("Paragraph links")

    class Kind(models.TextChoices):
        NEXT = "next", _("next")
        SIMILAR = "similar", _("similar")

    source = models.ForeignKey(Paragraph, on_delete=models.CASCADE, related_name="links")
    target = models.ForeignKey(Paragraph, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: Cosine similarity, for `similar`.
    score = models.FloatField(null=True)
