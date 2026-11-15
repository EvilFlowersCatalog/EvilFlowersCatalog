from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class ContentPolicy(BaseModel):
    """One version of a content policy document (#174).

    A save never edits a row: it writes the next version, which takes effect at once, and older
    versions stay readable so a receipt or log line pinned to `policy:key@version` can be explained
    later. The current policy for a key is its highest version. Catalogs and entries refer to a
    policy by key, never by version.
    """

    class Meta:
        app_label = "content"
        db_table = "content_policy"
        default_permissions = ()
        verbose_name = _("Content policy")
        verbose_name_plural = _("Content policies")
        constraints = [models.UniqueConstraint(fields=["key", "version"], name="content_policy_key_version")]
        indexes = [models.Index(fields=["key", "-version"])]

    key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    document = models.JSONField()
    updated_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return f"{self.key}@{self.version}"


__all__ = ["ContentPolicy"]
