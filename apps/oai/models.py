from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils.translation import gettext_lazy as _


class OaiRecord(models.Model):
    """One row per entry that has ever been harvestable (IP-033). Harvests read only this table.

    `entry_id` is deliberately not a foreign key: a tombstone (`deleted`, `projection` null) must outlive the
    entry and its catalog, because `Identify` promises `deletedRecord=persistent`. `datestamp` moves only when
    the `fingerprint` of the projection changes (`apps.oai.sync`), never on a plain save of the entry.
    """

    class Meta:
        app_label = "oai"
        db_table = "oai_records"
        default_permissions = ()
        verbose_name = _("OAI record")
        verbose_name_plural = _("OAI records")
        indexes = [
            # The keyset walk of every list verb: ORDER BY datestamp, id.
            models.Index(fields=["datestamp", "id"], name="oai_records_datestamp_id"),
            GinIndex(fields=["set_specs"], name="oai_records_set_specs_gin"),
        ]

    id = models.BigAutoField(primary_key=True)
    entry_id = models.UUIDField(unique=True)
    set_specs = ArrayField(models.TextField(), default=list)
    #: Whole seconds, UTC; written by the database clock in its own autocommit statement.
    datestamp = models.DateTimeField()
    deleted = models.BooleanField(default=False)
    #: SHA-256 of the canonical projection, setSpecs and crosswalk version (`apps.oai.projection.fingerprint`).
    fingerprint = models.CharField(max_length=64)
    projection = models.JSONField(null=True)

    def __str__(self):
        return f"{self.entry_id} ({'deleted' if self.deleted else 'live'})"


__all__ = ["OaiRecord"]
