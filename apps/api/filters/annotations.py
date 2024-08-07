import django_filters

from apps.core.models import Annotation, AnnotationItem


class AnnotationFilter(django_filters.FilterSet):
    user_acquisition_id = django_filters.UUIDFilter(lookup_expr="exact")
    title = django_filters.CharFilter(lookup_expr="icontains")
    user_id = django_filters.UUIDFilter(lookup_expr="exact", field_name="user_acquisition__user_id")

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


class AnnotationItemFilter(django_filters.FilterSet):
    annotation_id = django_filters.UUIDFilter(lookup_expr="exact")
    page_number = django_filters.NumberFilter(field_name="page")

    class Meta:
        model = AnnotationItem
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(annotation__user_acquisition__user=self.request.user)

        return qs
