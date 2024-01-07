from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.core.models import Entry
from apps.core.models.acquisition import Acquisition
from apps.core.models.user import User
from apps.core.models.base import BaseModel
from django.utils.translation import gettext as _


class UserAcquisition(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "user_acquisitions"
        default_permissions = ()
        verbose_name = _("User acquisition")
        verbose_name_plural = _("User acquisitions")

    class UserAcquisitionType(models.TextChoices):
        SHARED = "shared", _("Shared")
        PERSONAL = "personal", _("Personal")

    acquisition = models.ForeignKey(Acquisition, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    type = models.CharField(max_length=10, choices=UserAcquisitionType.choices, default=UserAcquisitionType.PERSONAL)
    range = models.CharField(max_length=100, null=True)
    expire_at = models.DateTimeField(null=True)

    @property
    def url(self) -> str:
        return (
            f"{settings.BASE_URL}" f"{reverse('user-acquisition-download', kwargs={'user_acquisition_id': self.pk})}"
        )

    @property
    def entry(self) -> Entry:
        # Because of the serializers
        return self.acquisition.entry
