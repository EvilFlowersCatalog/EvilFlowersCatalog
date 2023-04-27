import django_filters

from apps.core.models import Annotation


class AnnotationFilter(django_filters.FilterSet):
    user_acquisition_id = django_filters.UUIDFilter(lookup_expr='iexact')
    user_id = django_filters.UUIDFilter(lookup_expr='iexact', field_name='user_acquisition__user_id')

    class Meta:
        model = Annotation
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(user_acquisition__user=self.request.user)

        return qs
