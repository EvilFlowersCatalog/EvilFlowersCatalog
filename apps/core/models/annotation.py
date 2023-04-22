from django.db import models

from apps.core.models.base import BaseModel
from django.utils.translation import gettext as _

from apps.core.models.user_acquisition import UserAcquisition


class Annotation(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'annotations'
        default_permissions = ()
        verbose_name = _('Annotation')
        verbose_name_plural = _('Annotations')

    user_acquisition = models.ForeignKey(UserAcquisition, on_delete=models.CASCADE)
    content = models.TextField()
