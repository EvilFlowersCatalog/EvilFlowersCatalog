import django_filters
from django.db.models import Q

from apps.core.models import UserCatalog
from apps.harvest.models import HarvestRecord, HarvestSource


def manageable_sources(user):
    """Sources whose target catalog `user` manages; every source for a superuser; none for anonymous."""
    queryset = HarvestSource.objects.select_related("target_catalog")
    if not user.is_authenticated:
        return queryset.none()
    if user.is_superuser:
        return queryset
    return queryset.filter(
        target_catalog__user_catalogs__user=user, target_catalog__user_catalogs__mode=UserCatalog.Mode.MANAGE
    ).distinct()


class HarvestSourceFilter(django_filters.FilterSet):
    target_catalog_id = django_filters.UUIDFilter(help_text="Sources harvesting into this catalog.")
    adapter = django_filters.CharFilter(help_text="A registered adapter name (`GET /api/v1/harvest/adapters`).")
    enabled = django_filters.BooleanFilter()
    query = django_filters.CharFilter(method="filter_query", help_text="Slug or name contains.")

    class Meta:
        model = HarvestSource
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(Q(slug__icontains=value) | Q(name__icontains=value))


class HarvestRecordFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(
        choices=HarvestRecord.Status.choices, help_text="One or more statuses (repeat the parameter)."
    )
    dry_run = django_filters.BooleanFilter(help_text="true: rows of the last dry run; false: the mirror of real runs.")
    query = django_filters.CharFilter(method="filter_query", help_text="Record id or title contains.")

    class Meta:
        model = HarvestRecord
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(Q(record_id__icontains=value) | Q(source_record__title__icontains=value))


__all__ = ["HarvestRecordFilter", "HarvestSourceFilter", "manageable_sources"]
