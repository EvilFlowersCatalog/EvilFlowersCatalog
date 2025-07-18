import django_filters

from apps.core.models import UserAcquisition


class UserAcquisitionFilter(django_filters.FilterSet):
    """
    User acquisition filtering system for tracking user content access permissions.
    
    Provides filtering capabilities for user acquisitions - records of user access
    to content including purchases, loans, and subscriptions. Supports filtering
    by user, content, access type, and time-based constraints.
    """
    
    user_id = django_filters.UUIDFilter(
        help_text="Filter user acquisitions by user UUID. Returns access records for the specified user.",
    )
    acquisition_id = django_filters.UUIDFilter(
        help_text="Filter user acquisitions by acquisition UUID. Returns access records for the specified acquisition format.",
    )
    type = django_filters.CharFilter(
        lookup_expr="iexact",
        help_text="Filter user acquisitions by access type using case-insensitive exact matching. Types include purchase, loan, subscription, etc.",
    )
    title = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        field_name="acquisition__entry__title",
        help_text="Filter user acquisitions by content title using case-insensitive partial matching. Searches within entry titles.",
    )
    expire_at__gte = django_filters.DateTimeFilter(
        field_name="expire_at",
        lookup_expr="gte",
        help_text="Filter user acquisitions by expiration date. Returns access records that expire on or after the specified datetime.",
    )
    expire_at__lte = django_filters.DateTimeFilter(
        field_name="expire_at",
        lookup_expr="lte",
        help_text="Filter user acquisitions by expiration date. Returns access records that expire on or before the specified datetime.",
    )
    created_at__gte = django_filters.DateTimeFilter(
        field_name="created_at",
        lookup_expr="gte",
        help_text="Filter user acquisitions by creation date. Returns access records created on or after the specified datetime.",
    )
    created_at__lte = django_filters.DateTimeFilter(
        field_name="created_at",
        lookup_expr="lte",
        help_text="Filter user acquisitions by creation date. Returns access records created on or before the specified datetime.",
    )

    class Meta:
        model = UserAcquisition
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(user=self.request.user)

        return qs
