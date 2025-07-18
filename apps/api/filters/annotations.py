import django_filters

from apps.core.models import Annotation, AnnotationItem


class AnnotationFilter(django_filters.FilterSet):
    """
    Annotation filtering system for user content annotations and highlights.
    
    Provides filtering capabilities for annotations - user-created notes, highlights,
    and bookmarks on content. Supports filtering by user, acquisition, and content.
    """
    
    user_acquisition_id = django_filters.UUIDFilter(
        lookup_expr="exact",
        help_text="Filter annotations by user acquisition UUID. Returns annotations for the specified user's access to content.",
    )
    title = django_filters.CharFilter(
        lookup_expr="icontains",
        help_text="Filter annotations by title using case-insensitive partial matching. Searches within annotation titles and descriptions.",
    )
    user_id = django_filters.UUIDFilter(
        lookup_expr="exact",
        field_name="user_acquisition__user_id",
        help_text="Filter annotations by user UUID. Returns annotations created by the specified user.",
    )

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
    """
    Annotation item filtering system for individual annotation components.
    
    Provides filtering capabilities for annotation items - specific highlights,
    notes, or bookmarks within an annotation. Supports filtering by parent
    annotation and page location.
    """
    
    annotation_id = django_filters.UUIDFilter(
        lookup_expr="exact",
        help_text="Filter annotation items by parent annotation UUID. Returns items belonging to the specified annotation.",
    )
    page_number = django_filters.NumberFilter(
        field_name="page",
        help_text="Filter annotation items by page number. Returns items located on the specified page of the content.",
    )

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
