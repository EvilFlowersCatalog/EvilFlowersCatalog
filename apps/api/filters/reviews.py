import django_filters
from django.db.models import Q

from apps.core.models import EntryReview, UserCatalog


class EntryReviewFilter(django_filters.FilterSet):
    """
    Reviews across entries for moderators (IP-023): superusers see every review, catalog managers the
    reviews of entries in the catalogs they manage. Everyone else gets nothing (the view answers 403).
    """

    state = django_filters.ChoiceFilter(
        choices=EntryReview.State.choices, help_text="Only reviews in this state (`published` or `hidden`)."
    )
    catalog_id = django_filters.UUIDFilter(
        field_name="entry__catalog_id", help_text="Reviews of entries in this catalog."
    )
    entry_id = django_filters.UUIDFilter(help_text="Reviews of this entry.")
    user_id = django_filters.UUIDFilter(help_text="Reviews by this user.")
    rating = django_filters.NumberFilter(help_text="Reviews with exactly this rating (1-5).")
    rating__lte = django_filters.NumberFilter(field_name="rating", lookup_expr="lte", help_text="Rating at most.")
    query = django_filters.CharFilter(
        method="filter_query",
        help_text="Case-insensitive text in the entry title, the reviewer's name or username, or the review title "
        "or text.",
    )

    class Meta:
        model = EntryReview
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        value = (value or "").strip()
        if not value:
            return qs
        return qs.filter(
            Q(entry__title__icontains=value)
            | Q(user__name__icontains=value)
            | Q(user__surname__icontains=value)
            | Q(user__username__icontains=value)
            | Q(title__icontains=value)
            | Q(body__icontains=value)
        )

    @property
    def qs(self):
        qs = super().qs
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        managed = UserCatalog.objects.filter(user=user, mode=UserCatalog.Mode.MANAGE).values("catalog_id")
        return qs.filter(entry__catalog_id__in=managed)
