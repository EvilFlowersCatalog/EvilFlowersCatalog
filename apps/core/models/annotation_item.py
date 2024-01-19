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

    annotation = models.ForeignKey(Annotation, on_delete=models.CASCADE)
    page = models.PositiveSmallIntegerField()
    content = models.TextField()
