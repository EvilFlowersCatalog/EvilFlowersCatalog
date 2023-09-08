import django_filters
from django.db.models import Q
from django.utils.translation import gettext as _

from apps.core.models import ShelfRecord


class ShelfRecordFilter(django_filters.FilterSet):
    class Meta:
        model = ShelfRecord
        fields = []

    catalog_id = django_filters.UUIDFilter()
    catalog_title = django_filters.CharFilter(field_name='entry__catalog__title', lookup_expr='unaccent__icontains')
    entry_id = django_filters.UUIDFilter()
    user_id = django_filters.UUIDFilter()
    author_id = django_filters.UUIDFilter(method='filter_author_id', label=_("Author"))
    author = django_filters.CharFilter(method='filter_author')
    category_id = django_filters.UUIDFilter(label=_("Category"), field_name='entry__categories__id')
    category_term = django_filters.CharFilter(field_name='entry__categories__term')
    language_id = django_filters.UUIDFilter()
    language_code = django_filters.CharFilter(field_name='entry__language__code', label=_("Language"))
    title = django_filters.CharFilter(field_name='entry__title', lookup_expr='unaccent__icontains')
    summary = django_filters.CharFilter(field_name='entry__summary', lookup_expr='unaccent__icontains')
    query = django_filters.CharFilter(method='filter_name')
    feed_id = django_filters.UUIDFilter(field_name='entry__feeds__id')

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(
                Q(entry__catalog__users=self.request.user) & Q(user=self.request.user)
            )

        return qs

    @staticmethod
    def filter_author(qs, name, value):
        return qs.filter(
            Q(entry__author__name__unaccent__icontains=value)
            | Q(entry__author__surname__unaccent__icontains=value)
            | Q(entry__contributors__name__unaccent__icontains=value)
            | Q(entry__contributors__surname__unaccent__icontains=value)
        )

    @staticmethod
    def filter_author_id(qs, name, value):
        return qs.filter(
            Q(entry__author_id=value) | Q(entry__contributors__id=value)
        )

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(
            Q(entry__title__unaccent__icontains=value) | Q(entry__summary__unaccent__icontains=value)
        )
