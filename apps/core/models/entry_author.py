from django.db import models
from django.utils.translation import gettext as _


class EntryAuthor(models.Model):
    class Meta:
        app_label = "core"
        db_table = "entry_authors"
        default_permissions = ()
        verbose_name = _("Entry author")
        verbose_name_plural = _("Entry authors")
        unique_together = (("entry", "author"),)
        ordering = [
            "position",
        ]

    entry = models.ForeignKey("Entry", on_delete=models.CASCADE, related_name="entry_authors")
    author = models.ForeignKey("Author", on_delete=models.CASCADE, related_name="entry_authors")
    position = models.PositiveSmallIntegerField()


__all__ = ["EntryAuthor"]
