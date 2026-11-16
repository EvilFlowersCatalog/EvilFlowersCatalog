from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class Catalog(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "catalogs"
        default_permissions = ()
        verbose_name = _("Catalog")
        verbose_name_plural = _("Catalogs")
        unique_together = ("creator_id", "title")
        indexes = [
            models.Index(fields=["creator_id"]),
            models.Index(fields=["url_name"]),
            models.Index(fields=["title"]),
            models.Index(fields=["is_public"]),
            models.Index(fields=["touched_at"]),
            models.Index(fields=["is_public", "title"]),
            models.Index(fields=["creator_id", "is_public"]),
        ]

    creator = models.ForeignKey("User", on_delete=models.CASCADE)
    url_name = models.SlugField(unique=True)
    title = models.CharField(max_length=100)
    users = models.ManyToManyField("User", related_name="catalogs", through="UserCatalog")
    is_public = models.BooleanField(default=False)
    touched_at = models.DateTimeField(null=True, auto_now=True)
    #: Key of a content policy (`apps.content`, #174) for the entries of this catalog; null inherits the
    #: instance setting `content.default_policy`. A key, not a foreign key: policies are versioned rows.
    content_policy = models.CharField(max_length=100, null=True, blank=True)
    #: IP-033 Q1: the catalog's entries are served over OAI-PMH (`/oai`). Off by default.
    harvestable = models.BooleanField(default=False)


__all__ = [
    "Catalog",
]
