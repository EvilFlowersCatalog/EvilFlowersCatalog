"""Extraction output of a PDF acquisition (IP-018).

The catalog owns this schema and nothing else of extraction: the
`evilflowers-*-service` containers do the work and post their results to the
ingestion endpoints (`apps.extraction.views`). Positions are against the
original file: 1-based page, boxes as fractions of the page, origin top-left.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _
from pgvector.django import HalfVectorField, HnswIndex

from apps.core.models import Acquisition
from apps.core.models.base import BaseModel

#: One embedding model for entries, paragraphs and queries (IP-021): multilingual-e5-large-instruct,
#: 1024 dimensions, served by `tei-embed`. Stored as `halfvec`. A model of another dimension needs a
#: migration of these columns and a pipeline version bump.
EMBEDDING_DIMENSIONS = 1024


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
        indexes = [
            HnswIndex(name="extraction_run_embedding", fields=["embedding"], opclasses=["halfvec_cosine_ops"]),
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
    #: Bumped by every `/start` and every reset (#140). Services send the attempt they work for; results of an
    #: earlier attempt are refused as `superseded`, and a long job polls it to stop before doing the work.
    attempt = models.PositiveIntegerField(default=0)
    #: Image tags and digests, model names and versions that produced the run.
    provenance = models.JSONField(default=dict)
    error = models.TextField(null=True)
    #: The publication's vector: mean of its paragraph embeddings, for recommendations (IP-018 Phase 8).
    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    #: `POST_PROCESS_VERSION` (`apps.extraction.tasks`) of the post-processing that last succeeded on this run in
    #: full; null for none. The sweep (#155) post-processes every active run whose marker is not current.
    post_processed = models.PositiveSmallIntegerField(null=True)

    @property
    def storage_dir(self) -> str:
        """Where the services write this run's crops, relative to the storage root."""
        return storage_dir(self.acquisition, self.pipeline_version)


def storage_dir(acquisition: Acquisition, pipeline_version: int) -> str:
    return f"{acquisition.upload_base_path()}/extraction/{acquisition.pk}/v{pipeline_version}"


class RegionQuerySet(models.QuerySet):
    def visible(self) -> "RegionQuerySet":
        """What readers see (#189): every region but those a decider banded `hide`. Unknown (null) stays."""
        return self.exclude(band=Region.Band.HIDE)


class Region(BaseModel):
    """A figure, table or equation on a page of the original file."""

    objects = RegionQuerySet.as_manager()

    class Meta:
        app_label = "extraction"
        db_table = "extraction_region"
        default_permissions = ()
        ordering = ["page", "seq"]
        indexes = [
            models.Index(fields=["run", "page"]),
            HnswIndex(name="extraction_region_embedding", fields=["embedding"], opclasses=["halfvec_cosine_ops"]),
        ]
        verbose_name = _("Region")
        verbose_name_plural = _("Regions")

    class Kind(models.TextChoices):
        FIGURE = "figure", _("figure")
        TABLE = "table", _("table")
        EQUATION = "equation", _("equation")

    class Band(models.TextChoices):
        """What a decision model made of the region (#189): shown, shown but doubtful, hidden from readers."""

        KEEP = "keep", _("keep")
        REVIEW = "review", _("review")
        HIDE = "hide", _("hide")

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
    #: What is embedded and searched (IP-030 §3): the printed caption, the first paragraph that refers to the
    #: region, and the description once there is one (`apps.extraction.regions.document_text`).
    embedding_text = models.TextField(null=True)
    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True)
    #: `model@revision` of `embedding`: vectors of two models are never compared.
    embedding_model = models.CharField(max_length=200, null=True)
    #: `Locator.key` (#171): the address of this figure that survives a newer run.
    locator_key = models.CharField(max_length=40, null=True, db_index=True)

    # The decision a worker sent with the region (#189, knowledge `Proposals/Decision-Models.md`). Null: no
    # decision (the worker predates it, or the decider was down); such a region is shown as before.
    #: `keep` / `review` / `hide`; readers skip `hide` (`Region.objects.visible()`), nothing is deleted.
    band = models.CharField(max_length=10, choices=Band.choices, null=True)
    #: The calibrated probability of the chosen answer, 0-1 (wire name `p`), kept so a new threshold re-bands
    #: without re-extraction (`manage.py reband_regions`).
    decision_p = models.FloatField(null=True)
    #: The table score text-service computes from grounding, grid signals, review outcome and p, 0-1.
    decision_score = models.FloatField(null=True)
    #: The answer itself (`stamp`, `chart`, `repeated`, ...).
    decision = models.CharField(max_length=100, null=True)
    #: `model@revision` of the decider (the answer's top-level `model`).
    decider_model = models.CharField(max_length=200, null=True)
    #: Further signals behind the decision: `review_outcome`, `grounded_share`, grid signals of a table.
    decision_signals = models.JSONField(default=dict)


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
                opclasses=["halfvec_cosine_ops"],
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
    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True)
    #: `Locator.key` (#171): the address of this paragraph that survives a newer run.
    locator_key = models.CharField(max_length=40, null=True, db_index=True)


class Locator(BaseModel):
    """A run-independent address of a paragraph, section, figure, table or equation (#171).

    Runs replace each other and take their paragraphs and regions along; a citation, a section link or a graph
    key that pointed at a row id dies with it. A locator is keyed by what the unit *is* (its page and text, its
    heading, its caption and box: `apps.extraction.locators`), so the same paragraph in a newer run gets the same
    key. Locators are never deleted with a run. One no longer found in the active run is marked `detached_at` and
    still resolves, by its quote (a TextQuoteSelector: `quote_exact` with `prefix`/`suffix`) and then its page.

    Named *locator*, not *anchor*: `Reference.anchor` already means the printed label.
    """

    class Meta:
        app_label = "extraction"
        db_table = "extraction_locator"
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=["entry", "key"], name="extraction_locator_key")]
        verbose_name = _("Locator")
        verbose_name_plural = _("Locators")

    class Kind(models.TextChoices):
        PARAGRAPH = "paragraph", _("paragraph")
        SECTION = "section", _("section")
        FIGURE = "figure", _("figure")
        TABLE = "table", _("table")
        EQUATION = "equation", _("equation")
        #: An item of the reference list (#178).
        BIBITEM = "bibitem", _("reference list item")

    key = models.CharField(max_length=40)
    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: 1-based page of the original file where the unit was last seen.
    source_page = models.PositiveIntegerField()
    #: The unit's text (or caption, or heading), normalised, at most `locators.QUOTE_CHARS` characters.
    quote_exact = models.TextField(blank=True, default="")
    #: Text right before and after the quote (the neighbouring paragraphs), for a relocation that has to choose.
    prefix = models.TextField(blank=True, default="")
    suffix = models.TextField(blank=True, default="")
    #: Runs the key was first and last found in. Plain ids: runs are deleted, locators are not.
    first_run = models.UUIDField(null=True)
    last_run = models.UUIDField(null=True)
    #: When a newer run no longer had the key; null while the active run has it.
    detached_at = models.DateTimeField(null=True)


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


class SectionSimilarity(BaseModel):
    """Two sections of different publications that discuss the same thing (#151).

    text-service links `similar` paragraphs only inside one run, so cross-book similarity is computed here, by
    `apps.extraction.similarity`, from the section centroids of the paragraph embeddings. One row per unordered
    pair; the graph projects them as `SIMILAR_TO` and is rebuilt from this table, never recomputed.
    """

    class Meta:
        app_label = "extraction"
        db_table = "extraction_section_similarity"
        default_permissions = ()
        indexes = [
            models.Index(fields=["source"], name="extraction_section_sim_source"),
            models.Index(fields=["target"], name="extraction_section_sim_target"),
        ]
        verbose_name = _("Section similarity")
        verbose_name_plural = _("Section similarities")

    source = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="section_similarities")
    #: `Paragraph.section` of the source run: the heading, as the outline names the section.
    source_section = models.TextField()
    target = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="+")
    target_section = models.TextField()
    #: Cosine similarity of the two section centroids after the corpus mean is subtracted, 0-1.
    score = models.FloatField()


class Reference(BaseModel):
    """A paragraph's cross-reference to a figure, table, equation or section of the same run (IP-030 §4).

    Found by `apps.extraction.references` in the paragraph text ("Obr. 2.1", "Table 4", "Eq. (5)",
    "Section 2.3"). A reference whose target could not be found is kept with no target, so the patterns can be
    improved from the counts. Belongs to the run: a new run replaces its references with its paragraphs.
    """

    class Meta:
        app_label = "extraction"
        db_table = "extraction_reference"
        default_permissions = ()
        indexes = [models.Index(fields=["run", "kind"])]
        verbose_name = _("Reference")
        verbose_name_plural = _("References")

    class Kind(models.TextChoices):
        FIGURE = "figure", _("figure")
        TABLE = "table", _("table")
        EQUATION = "equation", _("equation")
        SECTION = "section", _("section")
        #: An in-text citation (`[12]`, "Smith 2019") of an item of the reference list (#178).
        BIBITEM = "bibitem", _("bibliography item")

    run = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="references")
    paragraph = models.ForeignKey(Paragraph, on_delete=models.CASCADE, related_name="references")
    region = models.ForeignKey(Region, on_delete=models.CASCADE, null=True, related_name="references")
    target_paragraph = models.ForeignKey(Paragraph, on_delete=models.CASCADE, null=True, related_name="+")
    bibitem = models.ForeignKey("extraction.Bibitem", on_delete=models.CASCADE, null=True, related_name="references")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: The text as written ("Obr. 2.1").
    anchor = models.CharField(max_length=100)
    #: The number referred to, normalised ("2.1").
    number = models.CharField(max_length=30)


class Bibitem(BaseModel):
    """One item of a run's reference list (#178), parsed by `apps.extraction.bibliography`.

    Belongs to the run, like its paragraphs; `locator_key` addresses it across runs (`Locator` of kind
    `bibitem`). `entry` is the catalog publication it cites, matched by DOI or ISBN: the graph's `CITES` edge.
    """

    class Meta:
        app_label = "extraction"
        db_table = "extraction_bibitem"
        default_permissions = ()
        ordering = ["run", "seq"]
        indexes = [models.Index(fields=["run", "seq"]), models.Index(fields=["doi"])]
        verbose_name = _("Bibliography item")
        verbose_name_plural = _("Bibliography items")

    run = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="bibitems")
    #: The reference list it belongs to (a book has one per chapter as often as one at the end), 0-based.
    block = models.PositiveIntegerField(default=0)
    seq = models.PositiveIntegerField()
    #: The printed number of a numbered list ("12").
    number = models.CharField(max_length=10, null=True)
    #: Page of the original file the item starts on.
    page = models.PositiveIntegerField()
    raw = models.TextField()
    #: CSL-JSON: title, author, issued, container-title, DOI, ISBN; Crossref fills more (`source: crossref`).
    csl = models.JSONField(default=dict)
    doi = models.CharField(max_length=255, null=True)
    isbn = models.CharField(max_length=13, null=True)
    entry = models.ForeignKey("core.Entry", on_delete=models.SET_NULL, null=True, related_name="cited_by")
    locator_key = models.CharField(max_length=40, null=True, db_index=True)
    #: When Crossref was asked about the DOI (whatever it answered).
    crossref_at = models.DateTimeField(null=True)
