from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import User
from apps.core.models.base import BaseModel


class ApiKey(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "api_keys"
        default_permissions = ("add",)
        verbose_name = _("API key")
        verbose_name_plural = _("API keys")

    class Processor(models.TextChoices):
        """Where the text a key reads ends up (#174): on our own hardware, or at a third-party model provider."""

        LOCAL = "local", _("local")
        EXTERNAL = "external", _("external")

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, null=True)
    last_seen_at = models.DateTimeField(null=True)
    is_active = models.BooleanField(default=True)
    #: Null inherits the instance setting `content.external_processor_default`.
    processor = models.CharField(max_length=10, choices=Processor.choices, null=True, blank=True)


__all__ = ["ApiKey"]
