import django_filters

from apps.core.models import Feed


class FeedFilter(django_filters.FilterSet):
    creator_id = django_filters.UUIDFilter()
    catalog_id = django_filters.UUIDFilter()
    title = django_filters.CharFilter(lookup_expr='unaccent__icontains')

    class Meta:
        model = Feed
        fields = []

    @property
    def qs(self):
        qs = super().qs
        user = getattr(self.request, 'user', None)

        if not user:
            return qs.none()

        if not user.is_superuser:
            qs = qs.filter(creator=user)

        return qs
