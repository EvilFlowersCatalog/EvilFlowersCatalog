import django_filters
from django.db.models import Q

from apps.core.models import Acquisition


class AcquisitionFilter(django_filters.FilterSet):
    """
    Acquisition filtering system for content access and download management.

    Provides filtering capabilities for acquisitions - the downloadable formats
    and access methods for catalog entries. Supports filtering by content type,
    acquisition relationship, and media format.
    """

    entry_id = django_filters.UUIDFilter(
        lookup_expr="exact",
        help_text="Filter acquisitions by entry UUID. Returns all available acquisition formats for the specified catalog entry.",
    )
    relation = django_filters.ChoiceFilter(
        choices=Acquisition.AcquisitionType.choices,
        help_text="Filter acquisitions by relationship type. Defines how the content can be accessed - direct download, streaming, purchase, borrow, etc.",
    )
    mime = django_filters.CharFilter(
        help_text="Filter acquisitions by MIME type. Specifies the media format (e.g., 'application/pdf', 'application/epub+zip', 'text/html')."
    )

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
