from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class ChapterSummary(BaseModel):
    """A short, generated summary of one chapter of a publication (IP-024 §7, #119).

    Chapters are the top level of `Entry.table_of_contents`; `page` is the reader's page the chapter starts on.
    A summary is generated once and kept while `digest` — the hash of the chapter's text and of the prompt that
    summarised it — stays the same. It is the assistant's own reading, never checked by a person, and is shown to
    the model as such. Publications lent through Readium LCP get none: their text never reaches the model.
    """

    class Meta:
        app_label = "assistant"
        db_table = "assistant_chapter_summaries"
        default_permissions = ()
        verbose_name = _("Chapter summary")
        verbose_name_plural = _("Chapter summaries")
        ordering = ["entry", "page"]
        constraints = [
            models.UniqueConstraint(fields=["entry", "page", "title"], name="assistant_chapter_summary_unique")
        ]

    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="chapter_summaries")
    title = models.TextField()
    page = models.PositiveIntegerField()
    #: Last reader page of the chapter.
    end_page = models.PositiveIntegerField()
    summary = models.TextField()
    digest = models.CharField(max_length=64)
    model = models.CharField(max_length=100, blank=True, default="")
    tokens_used = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.entry_id} p.{self.page}: {self.title[:40]}"
