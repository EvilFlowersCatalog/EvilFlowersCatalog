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
        # Only the slug identifies a feed (it is the OPDS path segment). Titles may
        # repeat — two "Novinky" feeds under different parents are legitimate.
        unique_together = (("catalog", "url_name"),)

    class FeedKind(models.TextChoices):
        NAVIGATION = "navigation", _("navigation")
        ACQUISITION = "acquisition", _("acquisition")

    class FeedSource(models.TextChoices):
        RELATION = "relation", _("relation")

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name="feeds")
    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    # Localized text, e.g. {"sk": "Nové knihy", "en": "New books"}
    title = models.JSONField(default=dict)
    url_name = models.SlugField()
    kind = models.CharField(max_length=20, choices=FeedKind.choices)
    source = models.CharField(max_length=20, choices=FeedSource.choices)
    content = models.TextField()
    per_page = models.IntegerField(null=True)
    entries = models.ManyToManyField(Entry, related_name="feeds", symmetrical=False)
    parents = models.ManyToManyField("self", related_name="children", db_table="feed_parents", symmetrical=False)
    touched_at = models.DateTimeField(null=True, auto_now=True)

    @property
    def url(self):
        return reverse("opds:feed", args=[self.catalog.url_name, self.url_name])

    @classmethod
    def available_url_name(cls, catalog, url_name: str, *, exclude_pk=None, max_length: int = 50) -> str:
        """First free slug in `catalog` built from `url_name`: itself, then `-2`, `-3`, ...

        Used to offer the client a concrete alternative when a slug is taken.
        """
        taken = cls.objects.filter(catalog=catalog, url_name__startswith=url_name[: max_length - 8])
        if exclude_pk is not None:
            taken = taken.exclude(pk=exclude_pk)
        taken = set(taken.values_list("url_name", flat=True))

        if url_name not in taken:
            return url_name

        counter = 2
        while True:
            suffix = f"-{counter}"
            candidate = f"{url_name[: max_length - len(suffix)]}{suffix}"
            if candidate not in taken:
                return candidate
            counter += 1


@receiver(post_save, sender=Feed)
def touch_catalog(sender, instance: Feed, **kwargs):
    instance.catalog.touched_at = timezone.now()
    instance.catalog.save()


__all__ = ["Feed"]
