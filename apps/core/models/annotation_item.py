from django.db import models

from apps.core.models.annotation import Annotation
from apps.core.models.base import BaseModel
from django.utils.translation import gettext as _


class AnnotationItem(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "annotation_items"
        default_permissions = ()
        verbose_name = _("Annotation Item")
        verbose_name_plural = _("Annotation Items")

    class Match(models.TextChoices):
        """How an agent quote was found in the PDF (IP-018 Quote resolution)."""

        EXACT = "exact", "exact"
        PARTIAL = "partial", "partial"

    annotation = models.ForeignKey(Annotation, on_delete=models.CASCADE)
    #: The reader's page number, as the viewer shows it.
    page = models.PositiveSmallIntegerField()
    content = models.TextField()

    # Agent highlights only: one item per quote (IP-018 Q9).
    seq = models.PositiveIntegerField(null=True, blank=True)
    match = models.CharField(max_length=16, choices=Match.choices, null=True, blank=True)
    #: One box per text line, `{x0, y0, x1, y1}` as page fractions, origin top-left.
    quads = models.JSONField(null=True, blank=True)
    #: The highlighted sentence, as the agent quoted it.
    text = models.TextField(null=True, blank=True)
    #: Why the agent marked it.
    note = models.TextField(null=True, blank=True)
