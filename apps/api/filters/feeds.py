import uuid

import django_filters
from django.core.exceptions import ValidationError
from django.forms import UUIDField

from apps.core.models import Feed


class FeedFilter(django_filters.FilterSet):
    creator_id = django_filters.UUIDFilter()
    catalog_id = django_filters.UUIDFilter()
    title = django_filters.CharFilter(lookup_expr="unaccent__icontains")
    kind = django_filters.ChoiceFilter(choices=Feed.FeedKind.choices)
    parent_id = django_filters.CharFilter(method="filter_parent_id")

    class Meta:
        model = Feed
        fields = []

    @staticmethod
    def filter_parent_id(qs, name, value):
        # TODO: this should be a convention
        if value == "null":
            return qs.filter(parents__isnull=True).distinct()
        else:
            try:
                value = uuid.UUID(value)
            except ValueError:
                raise ValidationError(UUIDField.default_error_messages["invalid"], code="invalid")
            return qs.filter(parents__id=value).distinct()

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.filter(catalog__is_public=True)

        if not self.request.user.is_superuser:
            # TODO: catalog access mode should be an integer so we can do gte a lte
            qs = qs.filter(
                catalog__user_catalogs__user=self.request.user,
                # catalog__user_catalogs__mode=UserCatalog.Mode.READ
            )

        return qs
