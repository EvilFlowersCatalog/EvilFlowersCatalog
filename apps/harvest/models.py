"""IP-034 Part A: harvest sources and the local mirror of their records.

A run is an IP-031 job (`harvest.run`), so there is no run model. `HarvestRecord` is the mirror: one row per
source record with what it said (`source_record`, `raw_hash`) and what the matcher decided (`status`, `entry`).
The link between a source record and an entry is `Entry.identifiers[source.system_key]`, not a foreign key.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel

#: Fields a harvest can write, each governed by `HarvestSource.field_policy` (default `fill_empty`).
POLICY_FIELDS = (
    "title",
    "authors",
    "publisher",
    "published_at",
    "language",
    "summary",
    "edition",
    "page_count",
    "identifiers",
    "subjects",
    "links",
)

#: `links` (MARC 856 URLs as external-URL acquisitions) are opt-in: the proposal stores them only "when the
#: source's field_policy allows links".
POLICY_DEFAULTS = {"links": "ignore"}


class HarvestSource(BaseModel):
    class Mode(models.TextChoices):
        CREATE = "create", _("Create")
        ENRICH = "enrich", _("Enrich")
        LINK_ONLY = "link_only", _("Link only")
        #: Two-way: three-way merge per field both ways; only for connectors that can write.
        SYNC = "sync", _("Two-way sync")

    class Policy(models.TextChoices):
        FILL_EMPTY = "fill_empty", _("Fill empty")
        OVERWRITE = "overwrite", _("Overwrite")
        IGNORE = "ignore", _("Ignore")

    class Meta:
        app_label = "harvest"
        db_table = "harvest_sources"
        default_permissions = ()
        verbose_name = _("Harvest source")
        verbose_name_plural = _("Harvest sources")

    #: `Entry.origin` is `harvest:<slug>` (80 characters), hence 72.
    slug = models.SlugField(max_length=72, unique=True)
    name = models.CharField(max_length=255)
    #: A name in the connector registry (`apps.harvest.adapters.registry`), validated there, not by `choices`:
    #: connectors of other apps register without a migration here.
    adapter = models.CharField(max_length=50)
    #: Connector-specific settings beyond the columns below. Never secrets: a connector names the setting that
    #: holds a token (`{"token_setting": "…"}`), the source form refuses credential-like keys.
    config = models.JSONField(default=dict, blank=True)
    base_url = models.URLField(max_length=1000, null=True, blank=True)
    metadata_prefix = models.CharField(max_length=50, default="marc21")
    set_spec = models.CharField(max_length=255, null=True, blank=True)
    #: The `Entry.identifiers` key the source's record ids go to (e.g. `arl_stu`).
    system_key = models.CharField(max_length=100)
    target_catalog = models.ForeignKey("core.Catalog", on_delete=models.CASCADE, related_name="harvest_sources")
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.LINK_ONLY)
    #: Per field (`POLICY_FIELDS`) `fill_empty` · `overwrite` · `ignore`.
    field_policy = models.JSONField(default=dict, blank=True)
    #: Five-field cron; empty: manual runs only.
    schedule = models.CharField(max_length=100, blank=True, default="")
    enabled = models.BooleanField(default=False)
    #: IP-033 Q7: whether entries this source created (`origin = harvest:<slug>`) are offered over Elvira's own
    #: OAI-PMH. False for sources that are OAI providers themselves.
    oai_reexpose = models.BooleanField(default=True)
    #: Last OAI `responseDate` of a complete run (the next run's `from`).
    cursor = models.CharField(max_length=40, null=True, blank=True)
    #: Not in the proposal's table: created entries and categories need a creator (both are required foreign
    #: keys), and the tick needs to know when the source last ran.
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    last_run_at = models.DateTimeField(null=True, blank=True)
    #: Counters of the last finished run, for catalog managers who cannot read `/api/v1/jobs`.
    last_report = models.JSONField(null=True, blank=True)
    #: Sources of an upload connector (`marc_file`): storage path of the uploaded file the next run reads.
    upload_path = models.CharField(max_length=500, null=True, blank=True)

    def policy(self, field: str) -> str:
        value = (self.field_policy or {}).get(field) or POLICY_DEFAULTS.get(field) or self.Policy.FILL_EMPTY
        return value if value in self.Policy.values else self.Policy.FILL_EMPTY

    @property
    def origin(self) -> str:
        return f"harvest:{self.slug}"

    @property
    def has_upload(self) -> bool:
        return bool(self.upload_path)

    def __str__(self):
        return self.slug


class SyncState(models.TextChoices):
    IN_SYNC = "in_sync", _("In sync")
    PENDING = "pending", _("Pending")
    CONFLICT = "conflict", _("Conflict")
    ERROR = "error", _("Error")


class HarvestRecord(BaseModel):
    class Status(models.TextChoices):
        MATCHED = "matched", _("Matched")
        CREATED = "created", _("Created")
        ENRICHED = "enriched", _("Enriched")
        AMBIGUOUS = "ambiguous", _("Ambiguous")
        UNMATCHED = "unmatched", _("Unmatched")
        #: #207: no identifier matched, but the decider named a likely entry; held (not created) until a
        #: librarian accepts (link) or rejects (create as new) the `MatchSuggestion`.
        SUGGESTED = "suggested", _("Suggested match")
        SKIPPED = "skipped", _("Skipped")
        FAILED = "failed", _("Failed")
        DELETED = "deleted", _("Deleted")

    class Meta:
        app_label = "harvest"
        db_table = "harvest_records"
        default_permissions = ()
        verbose_name = _("Harvest record")
        verbose_name_plural = _("Harvest records")
        constraints = [
            # `dry_run` is part of the key: a dry run must not overwrite the mirror row of the real run.
            models.UniqueConstraint(fields=["source", "record_id", "dry_run"], name="harvest_record_unique"),
        ]
        indexes = [
            models.Index(fields=["source", "dry_run", "status"], name="harvest_record_status_idx"),
        ]

    source = models.ForeignKey(HarvestSource, on_delete=models.CASCADE, related_name="records")
    record_id = models.CharField(max_length=255)
    datestamp = models.DateTimeField(null=True, blank=True)
    deleted = models.BooleanField(default=False)
    raw_hash = models.CharField(max_length=64, blank=True, default="")
    source_record = models.JSONField(default=dict)
    entry = models.ForeignKey("core.Entry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices)
    #: What the match was made on: the system key or an identifier type (`isbn`, `issn`, `ccnb`, `doi`).
    matched_on = models.CharField(max_length=100, null=True, blank=True)
    message = models.TextField(null=True, blank=True)
    dry_run = models.BooleanField(default=False)

    # -- `sync` sources ------------------------------------------------------------------------------------
    #: The canonical field values both sides agreed on at the last sync: the three-way merge's base.
    synced_record = models.JSONField(null=True, blank=True)
    #: The remote's version (opaque) and the entry's `updated_at` at that sync.
    remote_version = models.CharField(max_length=255, null=True, blank=True)
    local_version = models.DateTimeField(null=True, blank=True)
    sync_state = models.CharField(max_length=20, choices=SyncState.choices, null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.source_id}:{self.record_id} ({self.status})"


class HarvestConflict(BaseModel):
    """A field both sides changed since the last sync: the later side won, the losing value is kept here."""

    class Winner(models.TextChoices):
        LOCAL = "local", _("Local")
        REMOTE = "remote", _("Remote")

    class Meta:
        app_label = "harvest"
        db_table = "harvest_conflicts"
        default_permissions = ()
        verbose_name = _("Harvest conflict")
        verbose_name_plural = _("Harvest conflicts")
        indexes = [
            models.Index(fields=["source", "acknowledged", "-created_at"], name="harvest_conflict_source_idx"),
            models.Index(fields=["entry", "acknowledged"], name="harvest_conflict_entry_idx"),
        ]

    source = models.ForeignKey(HarvestSource, on_delete=models.CASCADE, related_name="conflicts")
    record = models.ForeignKey(HarvestRecord, on_delete=models.CASCADE, related_name="conflicts")
    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="+")
    field = models.CharField(max_length=50)
    local_value = models.JSONField(null=True, blank=True)
    remote_value = models.JSONField(null=True, blank=True)
    local_at = models.DateTimeField(null=True, blank=True)
    remote_at = models.DateTimeField(null=True, blank=True)
    winner = models.CharField(max_length=10, choices=Winner.choices)
    acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)


class MatchSuggestion(BaseModel):
    """#207: the decider's guess that a harvested record without a shared identifier is an entry already held.

    Never applied by itself (no `harvest_match` threshold exists until knowledge#65): the record waits as
    `suggested` and a librarian accepts (the record is linked to the entry) or rejects (the record is created as a
    new entry, in `create` mode). Accepted and rejected rows are labelled cases for calibration.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")
        #: An identifier matched on a later run before anyone decided.
        SUPERSEDED = "superseded", _("Superseded")

    class Meta:
        app_label = "harvest"
        db_table = "harvest_match_suggestions"
        default_permissions = ()
        verbose_name = _("Match suggestion")
        verbose_name_plural = _("Match suggestions")
        indexes = [models.Index(fields=["source", "status", "-created_at"], name="harvest_suggestion_queue_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["record"],
                condition=models.Q(status="pending"),
                name="harvest_suggestion_one_pending",
            )
        ]

    source = models.ForeignKey(HarvestSource, on_delete=models.CASCADE, related_name="match_suggestions")
    record = models.ForeignKey(HarvestRecord, on_delete=models.CASCADE, related_name="match_suggestions")
    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="+")
    #: Probability of the chosen candidate (uncalibrated until knowledge#65).
    p = models.FloatField(null=True)
    probabilities = models.JSONField(default=dict, blank=True)
    #: Entry ids the decider chose among, in the order offered.
    candidates = models.JSONField(default=list, blank=True)
    decider_model = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)


__all__ = [
    "POLICY_DEFAULTS",
    "POLICY_FIELDS",
    "HarvestConflict",
    "HarvestRecord",
    "HarvestSource",
    "MatchSuggestion",
    "SyncState",
]
