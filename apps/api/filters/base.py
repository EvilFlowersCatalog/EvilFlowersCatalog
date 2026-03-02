import django_filters
from django.core.cache import cache
from django.db.models import Q
from typing import Optional, List

from apps.core.models import Catalog, UserCatalog


class CachedSecuredFilterMixin:
    """
    Mixin that provides cached access control for filters.

    This mixin caches user permissions and accessible catalog IDs
    to avoid repeated database queries during filtering operations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._accessible_catalog_ids = None
        self._user_permission_cache = {}

    def get_accessible_catalog_ids(self) -> Optional[List[int]]:
        """
        Get list of catalog IDs accessible to the current user.
        Returns None for superusers (access to all catalogs).
        """
        if self._accessible_catalog_ids is not None:
            return self._accessible_catalog_ids

        if not self.request.user.is_authenticated:
            # Public catalogs only
            self._accessible_catalog_ids = list(Catalog.objects.filter(is_public=True).values_list("id", flat=True))
        elif self.request.user.is_superuser:
            # Superuser has access to all catalogs
            self._accessible_catalog_ids = None
        else:
            # Cache key for user's accessible catalogs
            cache_key = f"user_accessible_catalogs_{self.request.user.id}"
            cached_catalog_ids = cache.get(cache_key)

            if cached_catalog_ids is None:
                # Get user's assigned catalogs
                user_catalog_ids = list(
                    UserCatalog.objects.filter(user=self.request.user).values_list("catalog_id", flat=True)
                )

                # Get public catalogs
                public_catalog_ids = list(Catalog.objects.filter(is_public=True).values_list("id", flat=True))

                # Combine and deduplicate
                all_accessible_ids = list(set(user_catalog_ids + public_catalog_ids))

                # Cache for 5 minutes
                cache.set(cache_key, all_accessible_ids, 300)
                self._accessible_catalog_ids = all_accessible_ids
            else:
                self._accessible_catalog_ids = cached_catalog_ids

        return self._accessible_catalog_ids

    def apply_catalog_access_control(self, queryset):
        """
        Apply catalog access control to a queryset.

        This method filters the queryset to only include records
        from catalogs accessible to the current user.
        """
        catalog_ids = self.get_accessible_catalog_ids()

        if catalog_ids is None:
            # Superuser - no filtering needed
            return queryset

        # Filter by accessible catalog IDs
        return queryset.filter(catalog_id__in=catalog_ids)

    def apply_related_catalog_access_control(self, queryset, catalog_field_name="catalog"):
        """
        Apply catalog access control for models with related catalog fields.

        Args:
            queryset: The queryset to filter
            catalog_field_name: Name of the catalog field (default: "catalog")
        """
        catalog_ids = self.get_accessible_catalog_ids()

        if catalog_ids is None:
            # Superuser - no filtering needed
            return queryset

        # Filter by accessible catalog IDs through related field
        filter_kwargs = {f"{catalog_field_name}_id__in": catalog_ids}
        return queryset.filter(**filter_kwargs)


class BaseSecuredFilter(CachedSecuredFilterMixin, django_filters.FilterSet):
    """
    Base filter class with cached access control.

    All API filters should inherit from this class to get
    optimized permission checking and catalog access control.
    """

    pass
