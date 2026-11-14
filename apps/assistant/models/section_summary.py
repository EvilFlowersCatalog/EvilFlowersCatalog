from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class SectionSummary(BaseModel):
    """A short, generated summary of one section of a processed publication (IP-030 §4).

    Replaces the chapter summaries of IP-024 §7, which needed a table of contents (2 of 435 publications have
    one). Sections are the distinct `Paragraph.section` headings of the active extraction run, in reading order
    (`apps.assistant.sections.outline`). A summary is generated the first time a reader asks for the section and
    kept while `digest` — the hash of the section's text and of the prompt — stays the same; a new extraction
    run deletes it with the run. It also carries the section's `concepts`, which feed the AGE graph (Phase 3b).
    Publications lent through Readium LCP get none: their text never reaches the model.
    """

    class Meta:
        app_label = "assistant"
        db_table = "assistant_section_summary"
        default_permissions = ()
        verbose_name = _("Section summary")
        verbose_name_plural = _("Section summaries")
        ordering = ["entry", "first_page"]
        constraints = [models.UniqueConstraint(fields=["run", "section"], name="assistant_section_summary_unique")]

    run = models.ForeignKey("extraction.ExtractionRun", on_delete=models.CASCADE, related_name="section_summaries")
    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="section_summaries")
    section = models.TextField()
    #: Pages of the original file (as the viewer shows an open-access publication).
    first_page = models.PositiveIntegerField()
    last_page = models.PositiveIntegerField()
    summary = models.TextField()
    #: `[{"term": "...", "role": "defines" | "discusses"}]`, 3-8 per section.
    concepts = models.JSONField(default=list, blank=True)
    digest = models.CharField(max_length=64)
    model = models.CharField(max_length=100, blank=True, default="")
    tokens_used = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.entry_id} p.{self.first_page}: {self.section[:40]}"
