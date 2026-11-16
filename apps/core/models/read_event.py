from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import Entry


class ReadEvent(models.Model):
    """
    One publication opened or downloaded (#194). Aggregated for statistics; never shown per person.

    `UserActivity` cannot give a time series: it collapses repeats into one row, skips anonymous
    readers and is pruned after a year. This table is append-only and privacy-minimal instead:
    no user, no IP, no session. Repeated range or re-opened requests are collapsed *before* the
    insert (`ReadService.record`), with a short-lived keyed hash that lives only in the cache.
    """

    class Meta:
        app_label = "core"
        db_table = "read_events"
        default_permissions = ()
        verbose_name = _("Read event")
        verbose_name_plural = _("Read events")
        indexes = [
            models.Index(fields=["catalog_id", "occurred_at"], name="read_events_catalog_at_idx"),
            models.Index(fields=["occurred_at"], name="read_events_at_idx"),
        ]

    class Via(models.TextChoices):
        ACQUISITION = "acquisition", _("Acquisition download")
        USER_ACQUISITION = "user_acquisition", _("User acquisition download")
        LCP_LICENSE = "lcp_license", _("LCP license download")

    id = models.BigAutoField(primary_key=True)
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="+")
    # Denormalised for the statistics query: one index range scan per catalog and period.
    catalog_id = models.UUIDField()
    occurred_at = models.DateTimeField(default=timezone.now)
    via = models.CharField(max_length=16, choices=Via.choices)
    # No user FK on purpose: aggregate use only.
    authenticated = models.BooleanField()
