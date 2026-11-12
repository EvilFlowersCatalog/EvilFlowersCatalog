"""
Ratings and reviews of entries (IP-023).

Every write goes through `ReviewService` so the entry's aggregate (`rating_count`, `rating_sum`) is
recomputed from `entry_reviews` in the same transaction. It is never incremented, so it cannot drift;
`manage.py recompute_ratings` rebuilds it for all entries anyway. Hidden reviews do not count (Q2).
"""

from typing import Iterable, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Count, ExpressionWrapper, F, FloatField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from apps.core.models import Entry, EntryReview, Language, User, UserActivity, UserCatalog
from apps.core.services.activity import ActivityService


def can_read_entry(user, entry: Entry) -> bool:
    """The access rule of the entry lists (`BaseSecuredFilter`): public catalog, membership, or superuser."""
    if entry.catalog.is_public:
        return True
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return UserCatalog.objects.filter(user=user, catalog_id=entry.catalog_id).exists()


def can_moderate(user, entry: Entry) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return UserCatalog.objects.filter(user=user, catalog_id=entry.catalog_id, mode=UserCatalog.Mode.MANAGE).exists()


def own_ratings(user, entry_ids: Iterable) -> dict:
    """`{entry_id: rating}` of the caller's reviews among these entries (hidden ones included: they are theirs)."""
    if not user.is_authenticated:
        return {}
    return dict(EntryReview.objects.filter(user=user, entry_id__in=list(entry_ids)).values_list("entry_id", "rating"))


def rating_score():
    """Bayesian average for `order_by=rating` (Q8): the prior pulls entries with few ratings to the middle."""
    weight = settings.EVILFLOWERS_REVIEWS_PRIOR_WEIGHT
    mean = settings.EVILFLOWERS_REVIEWS_PRIOR_MEAN
    return ExpressionWrapper(
        (Value(weight * mean) + F("rating_sum")) / (Value(weight) + F("rating_count")),
        output_field=FloatField(),
    )


@receiver(post_delete, sender=EntryReview, dispatch_uid="reviews_recompute_on_delete")
def review_deleted(sender, instance: EntryReview, **kwargs):
    """Deletions that bypass `ReviewService` (a user's account erased, admin bulk delete) still update the
    entry's rating. A no-op for an entry that is being deleted itself."""
    ReviewService.recompute([instance.entry_id])


class ReviewService:
    @staticmethod
    def recompute(entry_ids: Iterable) -> None:
        """Set `rating_count` / `rating_sum` of these entries from their published reviews."""
        entry_ids = list(entry_ids)
        if not entry_ids:
            return
        published = EntryReview.objects.filter(entry=OuterRef("pk"), state=EntryReview.State.PUBLISHED)
        count = published.values("entry").annotate(value=Count("pk")).values("value")
        total = published.values("entry").annotate(value=Sum("rating")).values("value")
        Entry.objects.filter(pk__in=entry_ids).update(
            rating_count=Coalesce(Subquery(count), 0),
            rating_sum=Coalesce(Subquery(total), 0),
        )

    @classmethod
    @transaction.atomic
    def upsert(
        cls,
        user: User,
        entry: Entry,
        *,
        rating: int,
        title: str = "",
        body: str = "",
        language: Optional[Language] = None,
    ) -> tuple[EntryReview, bool]:
        """Create or replace `user`'s review of `entry`. A moderator's decision stays: an edited hidden
        review remains hidden until a moderator publishes it again."""
        review, created = EntryReview.objects.select_for_update().get_or_create(
            entry=entry,
            user=user,
            defaults={"rating": rating, "title": title, "body": body, "language": language},
        )
        if not created:
            review.rating = rating
            review.title = title
            review.body = body
            review.language = language
            review.save(update_fields=["rating", "title", "body", "language", "updated_at"])
        cls.recompute([entry.pk])
        ActivityService.record(
            user, entry, UserActivity.ActivityAction.ENTRY_REVIEWED, {"rating": rating, "created": created}
        )
        return review, created

    @classmethod
    @transaction.atomic
    def delete(cls, review: EntryReview) -> None:
        review.delete()  # `review_deleted` recomputes the entry's rating

    @classmethod
    @transaction.atomic
    def moderate(cls, review: EntryReview, moderator: User, *, state: str, note: Optional[str] = None) -> EntryReview:
        review.state = state
        review.moderated_by = moderator
        review.moderated_at = timezone.now()
        if note is not None:
            review.moderation_note = note
        review.save(update_fields=["state", "moderated_by", "moderated_at", "moderation_note", "updated_at"])
        cls.recompute([review.entry_id])
        return review

    @classmethod
    def recompute_all(cls, batch_size: int = 500) -> int:
        """Rebuild the aggregate of every entry; returns how many entries were checked."""
        ids = list(Entry.objects.values_list("pk", flat=True))
        for start in range(0, len(ids), batch_size):
            with transaction.atomic():
                cls.recompute(ids[start : start + batch_size])
        return len(ids)

    @staticmethod
    def drifted() -> int:
        """Entries whose stored aggregate differs from a recount (for `recompute_ratings --check`)."""
        published = Q(reviews__state=EntryReview.State.PUBLISHED)
        return (
            Entry.objects.annotate(
                real_count=Count("reviews", filter=published),
                real_sum=Coalesce(Sum("reviews__rating", filter=published), 0),
            )
            .exclude(rating_count=F("real_count"), rating_sum=F("real_sum"))
            .count()
        )
