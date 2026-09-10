"""Helpers shared by the management tools.

The important one is `resolve_catalog_for_management`. Every write in this
server is ultimately authorised against a *catalog*, and the order of the two
checks matters:

1. Can the caller **read** the catalog? If not, report "not found". Saying
   "insufficient permissions" would confirm that a catalog with that id exists,
   turning the tool into an existence oracle for other tenants.
2. Can the caller **manage** it? If not, report the permission failure — at
   this point the caller already knows the catalog exists.
"""

from django.http import QueryDict
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.catalogs import CatalogFilter
from apps.core.models import Catalog
from apps.mcp.errors import ToolNotFound, ToolPermissionDenied


def resolve_readable_catalog(request, catalog_id: str) -> Catalog:
    """The catalog, if this credential may read it. Otherwise `ToolNotFound`."""
    catalog = CatalogFilter(
        QueryDict(mutable=True), queryset=Catalog.objects.filter(pk=catalog_id), request=request
    ).qs.first()
    if catalog is None:
        raise ToolNotFound("Catalog", catalog_id)
    return catalog


def resolve_catalog_for_management(request, catalog_id: str, action: str) -> Catalog:
    """The catalog, if this credential may *manage* it.

    `action` is a verb phrase used in the error message ("create a feed in"), so
    a refusal tells the model what it was refused rather than just that it was.
    """
    catalog = resolve_readable_catalog(request, catalog_id)

    if not has_object_permission("check_catalog_manage", request.user, catalog):
        raise ToolPermissionDenied(action, f"catalog '{catalog.title}'")

    return catalog


__all__ = ["resolve_catalog_for_management", "resolve_readable_catalog"]
