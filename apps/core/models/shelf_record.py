from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import Entry, User
from apps.core.models.base import BaseModel


class ShelfRecord(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "shelf_records"
        default_permissions = ()
        verbose_name = _("Shelf record")
        verbose_name_plural = _("Shelf records")
        unique_together = (("entry", "user"),)

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="shelf_records")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="shelf_records")
