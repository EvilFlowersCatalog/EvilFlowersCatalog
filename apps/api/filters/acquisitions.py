import django_filters
from django.db.models import Q

from apps.core.models import Acquisition


class AcquisitionFilter(django_filters.FilterSet):
    entry_id = django_filters.UUIDFilter(lookup_expr="exact")
    relation = django_filters.ChoiceFilter(choices=Acquisition.AcquisitionType.choices)
    mime = django_filters.CharFilter()

    class Meta:
        model = Acquisition
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(Q(entry__catalog__users=self.request.user) | Q(entry__catalog__is_public=True))

        return qs
