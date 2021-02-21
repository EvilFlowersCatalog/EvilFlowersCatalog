import django_filters

from apps.core.models import Entry


class EntryFilter(django_filters.FilterSet):
    creator_id = django_filters.UUIDFilter()
    catalog_id = django_filters.UUIDFilter()
    author_id = django_filters.UUIDFilter()
    category_id = django_filters.UUIDFilter()
    language_id = django_filters.UUIDFilter()
    title = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    summary = django_filters.CharFilter(lookup_expr='unaccent__icontains')

    class Meta:
        model = Entry
        fields = []

    @property
    def qs(self):
        qs = super().qs
        api_key = getattr(self.request, 'api_key', None)

        if not api_key:
            return qs.none()

        if not api_key.user.is_superuser:
            qs = qs.filter(creator=api_key)

        return qs
