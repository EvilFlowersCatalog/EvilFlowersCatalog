from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class Band(models.TextChoices):
    """As on extraction regions (#189): what readers see of an answer."""

    KEEP = "keep", _("keep")
    REVIEW = "review", _("review")
    HIDE = "hide", _("hide")


class SectionRelation(BaseModel):
    """How section B relates to section A, as the decider answered for a `SectionSimilarity` pair (#190).

    Keyed by the graph's run-independent section keys (`apps.graph.projection.section_key`), not by the pair row:
    `SectionSimilarity` is deleted and written again by every post-processing and by the weekly rebuild, and its
    direction can flip. `a_key` is the A of the question; the relation is read from A to B.
    """

    class Meta:
        app_label = "decider"
        db_table = "decider_section_relation"
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=["a_key", "b_key"], name="decider_section_relation_pair")]
        indexes = [models.Index(fields=["b_key"], name="decider_section_relation_b")]
        verbose_name = _("Section relation")
        verbose_name_plural = _("Section relations")

    class Relation(models.TextChoices):
        SAME = "same", _("explains the same idea")
        PREREQUISITE = "pre", _("A is a prerequisite of B")
        EXAMPLE = "example", _("B is an example of A")
        LOOSE = "loose", _("loosely related")
        NONE = "none", _("unrelated")

    a_key = models.CharField(max_length=64)
    b_key = models.CharField(max_length=64)
    relation = models.CharField(max_length=10, choices=Relation.choices)
    #: Probability of `relation` (calibrated by the model, not by us until knowledge#65).
    p = models.FloatField(null=True)
    probabilities = models.JSONField(default=dict, blank=True)
    band = models.CharField(max_length=10, choices=Band.choices, default=Band.KEEP)
    #: `model` of the decider's answer; a pair typed by another model is asked again.
    decider_model = models.CharField(max_length=200)


class SubjectSuggestion(BaseModel):
    """A subject the decider suggested for an entry (#191): filed, waiting for a librarian, or not taken.

    One row per entry and model, also when nothing was filed: it marks the entry as asked by that model. It is the
    provenance of a category the decider filed (`entry_categories` has no column for it), and an accepted or
    rejected row is a labelled case for the next calibration.
    """

    class Meta:
        app_label = "decider"
        db_table = "decider_subject_suggestion"
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["entry", "decider_model"], name="decider_subject_suggestion_once")
        ]
        indexes = [models.Index(fields=["catalog", "status"], name="decider_subject_queue")]
        verbose_name = _("Subject suggestion")
        verbose_name_plural = _("Subject suggestions")

    class Status(models.TextChoices):
        #: Above the upper threshold: added to the entry's categories.
        FILED = "filed", _("filed")
        #: Between the thresholds (or no thresholds yet): a librarian decides.
        PENDING = "pending", _("pending review")
        ACCEPTED = "accepted", _("accepted")
        REJECTED = "rejected", _("rejected")
        #: Below the lower threshold, or the entry already had the category: nothing to do.
        SKIPPED = "skipped", _("skipped")

    entry = models.ForeignKey("core.Entry", on_delete=models.CASCADE, related_name="subject_suggestions")
    catalog = models.ForeignKey("core.Catalog", on_delete=models.CASCADE, related_name="+")
    category = models.ForeignKey("core.Category", on_delete=models.CASCADE, null=True, related_name="+")
    #: Joint probability: top level x second level when both were asked.
    p = models.FloatField(null=True)
    #: The answers that led here: `[{level, choice, p, probabilities}]`.
    trail = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    decider_model = models.CharField(max_length=200)
    decided_by = models.ForeignKey("core.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
