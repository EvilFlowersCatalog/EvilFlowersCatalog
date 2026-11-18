from datetime import datetime
from typing import Optional

from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Entry
from apps.core.models.acquisition import Acquisition
from apps.core.models.user import User
from apps.core.models.base import BaseModel
from django.utils.translation import gettext as _


class UserAcquisitionQuerySet(models.QuerySet):
    def live(self, at: Optional[datetime] = None) -> "UserAcquisitionQuerySet":
        """Grants that have not expired (#215): no `expire_at`, or one still in the future."""
        return self.filter(Q(expire_at__isnull=True) | Q(expire_at__gt=at or timezone.now()))

    def equivalent(self, user, acquisition, type: str, range: Optional[str]) -> "UserAcquisitionQuerySet":
        """Live grants that give the same access (IP-036): same user, acquisition, type and page range.

        Reused instead of creating another one; oldest first, so repeated lookups return the same row.
        """
        same_range = Q(range__isnull=True) | Q(range="") if not range else Q(range=range)
        return (
            self.live().filter(same_range, user=user, acquisition=acquisition, type=type).order_by("created_at", "pk")
        )


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
        LCP = "lcp", _("LCP Protected")

    acquisition = models.ForeignKey(Acquisition, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    type = models.CharField(
        max_length=10,
        choices=UserAcquisitionType.choices,
        default=UserAcquisitionType.PERSONAL,
    )
    range = models.CharField(max_length=100, null=True)
    #: The grant ends here (#215): every read path refuses it after, and reuse never hands it back.
    expire_at = models.DateTimeField(null=True)

    objects = UserAcquisitionQuerySet.as_manager()

    @property
    def is_expired(self) -> bool:
        return self.expire_at is not None and self.expire_at <= timezone.now()

    @property
    def url(self) -> str:
        return reverse("files:user-acquisition-download", kwargs={"user_acquisition_id": self.pk})

    @property
    def entry(self) -> Entry:
        # Because of the serializers
        return self.acquisition.entry
