from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models.entry import Entry
from apps.core.models.user import User
from apps.core.models.base import BaseModel
from apps.core.models.catalog import Catalog


class Feed(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "feeds"
        default_permissions = ()
        verbose_name = _("Feed")
        verbose_name_plural = _("Feeds")
        unique_together = (("catalog", "title"), ("catalog", "url_name"))

    class FeedKind(models.TextChoices):
        NAVIGATION = "navigation", _("navigation")
        ACQUISITION = "acquisition", _("acquisition")

    class FeedSource(models.TextChoices):
        RELATION = "relation", _("relation")

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name="feeds")
    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    url_name = models.SlugField()
    kind = models.CharField(max_length=20, choices=FeedKind.choices)
    source = models.CharField(max_length=20, choices=FeedSource.choices)
    content = models.TextField()
    per_page = models.IntegerField(null=True)
    entries = models.ManyToManyField(Entry, related_name="feeds")
    parents = models.ManyToManyField("self", related_name="children", db_table="feed_parents", symmetrical=False)
    touched_at = models.DateTimeField(null=True, auto_now=True)

    @property
    def url(self):
        return f"{settings.BASE_URL}{reverse('opds:feed', args=[self.catalog.url_name, self.url_name])}"


@receiver(post_save, sender=Feed)
def touch_catalog(sender, instance: Feed, **kwargs):
    instance.catalog.touched_at = timezone.now()
    instance.catalog.save()


__all__ = ["Feed"]
