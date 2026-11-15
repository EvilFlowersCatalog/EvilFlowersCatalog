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


class ContentGrant(BaseModel):
    """A fact about a reader and a publication: a Readium LCP licence or an own copy they hold or held (#175).

    One row per source (a licence, a `UserAcquisition`), kept in step by signals and never deleted with its
    source: an ended loan stays a fact. The row is not a verdict. The policy's `grants.*` keys decide which rows
    count, so a policy change applies at once, to every reader, without a backfill.
    """

    class Meta:
        app_label = "content"
        db_table = "content_grant"
        default_permissions = ()
        verbose_name = _("Content grant")
        verbose_name_plural = _("Content grants")
        constraints = [models.UniqueConstraint(fields=["source", "source_id"], name="content_grant_source")]
        indexes = [models.Index(fields=["user", "entry"])]

    class Source(models.TextChoices):
        LICENSE = "license", _("Readium LCP licence")
        ACQUISITION = "acquisition", _("Own copy")

    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="+")
    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="+")
    source = models.CharField(max_length=12, choices=Source.choices)
    #: The licence's or the `UserAcquisition`'s id; no foreign key, so the fact outlives its row.
    source_id = models.UUIDField()
    #: The licence state (`ready` … `cancelled`) or the copy's type (`personal`, `shared`, `lcp`).
    source_state = models.CharField(max_length=15)
    #: The page range of a copy (`UserAcquisition.range`); null for the whole publication.
    pages = models.CharField(max_length=100, null=True, blank=True)
    #: When the reader first opened the loan in a reading app (a device registered); null if never known.
    first_opened_at = models.DateTimeField(null=True, blank=True)
    granted_at = models.DateTimeField()
    #: The source was deleted (a copy removed); the grant no longer counts.
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.source}:{self.source_id} {self.source_state}"


class ContentDisclosure(BaseModel):
    """What content was handed to a machine caller: one row per passage, region or page (#175).

    Feeds the reading budget (`chars_charged` within the policy's `window_days`) and the audit trail; pruned
    after the policy's `log.retention_days` (never inside a budget window). The same event is logged as JSON.
    """

    class Meta:
        app_label = "content"
        db_table = "content_disclosure"
        default_permissions = ()
        verbose_name = _("Content disclosure")
        verbose_name_plural = _("Content disclosures")
        indexes = [models.Index(fields=["user", "entry", "created_at"]), models.Index(fields=["created_at"])]

    user = models.ForeignKey("core.User", null=True, on_delete=models.SET_NULL, related_name="+")
    api_key = models.ForeignKey("core.ApiKey", null=True, on_delete=models.SET_NULL, related_name="+")
    processor = models.CharField(max_length=10, null=True)
    surface = models.CharField(max_length=20)
    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="+")
    locator = models.CharField(max_length=40, null=True)
    tier = models.CharField(max_length=10)
    basis = models.CharField(max_length=10)
    chars = models.PositiveIntegerField()
    #: Characters counted against the reading budget: `chars` when the basis is metered, else 0.
    chars_charged = models.PositiveIntegerField(default=0)
    #: `key@version` of the policy that decided.
    policy = models.CharField(max_length=80)

    def __str__(self):
        return f"{self.entry_id} {self.locator or ''} {self.chars}"


class ContentPack(BaseModel):
    """The labelled passages `content_pack` handed to one caller (#176), kept so the labels can be checked later.

    Holds addresses (`entry_id`, `locator_key`), never text: reading a pack back passes the gate again under the
    reader's access at that time. Pruned with the disclosure log.
    """

    class Meta:
        app_label = "content"
        db_table = "content_pack"
        default_permissions = ()
        verbose_name = _("Content pack")
        verbose_name_plural = _("Content packs")
        indexes = [models.Index(fields=["created_at"])]

    key = models.CharField(max_length=24, unique=True)
    user = models.ForeignKey("core.User", null=True, on_delete=models.CASCADE, related_name="+")
    query = models.TextField()
    purpose = models.TextField(blank=True, default="")
    #: `[{label, entry_id, locator_key, kind, page}]` in label order.
    items = models.JSONField(default=list)

    def __str__(self):
        return self.key


__all__ = ["ContentDisclosure", "ContentGrant", "ContentPack", "ContentPolicy"]
