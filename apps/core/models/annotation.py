from django.db import models

from apps.core.models.base import BaseModel
from django.utils.translation import gettext_lazy as _

from apps.core.models.user_acquisition import UserAcquisition


class Annotation(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "annotations"
        default_permissions = ()
        verbose_name = _("Annotation")
        verbose_name_plural = _("Annotations")

    class Kind(models.TextChoices):
        """The viewer layer an annotation belongs to (IP-018)."""

        DRAWING = "drawing", _("Drawing")
        HIGHLIGHT = "highlight", _("Highlight")
        AGENT = "agent", _("Agent")
        BOOKMARKS = "bookmarks", _("Bookmarks")

    class Status(models.TextChoices):
        """Progress of an agent annotation; the reader's own ones are always `done`."""

        PENDING = "pending", _("Pending")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    title = models.CharField(max_length=255, default="Annotation")
    user_acquisition = models.ForeignKey(UserAcquisition, on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.DRAWING)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DONE)

    # Agent annotations only.
    prompt = models.TextField(null=True, blank=True)
    summary = models.TextField(null=True, blank=True)
    #: Quotes the agent returned that could not be found in the PDF.
    unresolved_count = models.PositiveIntegerField(null=True, blank=True)
