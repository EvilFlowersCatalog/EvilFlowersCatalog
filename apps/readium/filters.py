import django_filters
from django_filters import FilterSet

from apps.readium.models import License


class LicenseFilter(FilterSet):
    user_id = django_filters.UUIDFilter(lookup_expr="exact", field_name="user_id", label="User UUID")
    entry_id = django_filters.UUIDFilter(lookup_expr="exact", field_name="user_id", label="Entry UUID")
    state = django_filters.ChoiceFilter(choices=License.LicenseState.choices, label="The current state of the license")
    starts_at__gte = django_filters.IsoDateTimeFilter(
        lookup_expr="gte", field_name="starts_at", label="Licenses that started after the specific datetime (ISO8601)"
    )
    starts_at__lte = django_filters.IsoDateTimeFilter(
        lookup_expr="lte", field_name="starts_at", label="Licenses that started before the specific datetime (ISO8601)"
    )
    expires_at__gte = django_filters.IsoDateTimeFilter(
        lookup_expr="gte", field_name="starts_at", label="Licenses that expired after the specific datetime (ISO8601)"
    )
    expires_at__lte = django_filters.IsoDateTimeFilter(
        lookup_expr="lte", field_name="starts_at", label="Licenses that expired before the specific datetime (ISO8601)"
    )

    class Meta:
        model = License
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(user=self.request.user)

        return qs
