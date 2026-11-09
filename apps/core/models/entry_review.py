from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel
from apps.core.models.entry import Entry
from apps.core.models.language import Language
from apps.core.models.user import User


class EntryReview(BaseModel):
    """A reader's rating of an entry, with optional text (IP-023). One per (entry, user)."""

    class Meta:
        app_label = "core"
        db_table = "entry_reviews"
        default_permissions = ()
        verbose_name = _("Entry review")
        verbose_name_plural = _("Entry reviews")
        constraints = [models.UniqueConstraint(fields=["entry", "user"], name="entry_reviews_entry_user")]
        indexes = [models.Index(fields=["entry", "state", "-created_at"])]

    class State(models.TextChoices):
        PUBLISHED = "published", _("Published")
        HIDDEN = "hidden", _("Hidden")

    RATING_MIN = 1
    RATING_MAX = 5

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(RATING_MIN), MaxValueValidator(RATING_MAX)]
    )
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    language = models.ForeignKey(Language, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PUBLISHED)
    moderated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderation_note = models.TextField(blank=True, default="")

    @property
    def language_code(self):
        return self.language.alpha2 if self.language_id else None

    def __str__(self):
        return f"{self.rating}★ {self.entry_id} by {self.user_id}"


__all__ = ["EntryReview"]
