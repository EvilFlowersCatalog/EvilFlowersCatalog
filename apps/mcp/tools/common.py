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

from django.conf import settings
from django.http import QueryDict
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.catalogs import CatalogFilter
from apps.core.models import Catalog
from apps.mcp.errors import ToolError, ToolNotFound, ToolPermissionDenied


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


def access_mode(request, catalog: Catalog) -> str:
    """The strongest thing this credential may do with `catalog`.

    Reported by `get_catalog` and `list_catalogs` so a model can plan curation
    work from one call instead of discovering its limits through refusals.
    """
    if has_object_permission("check_catalog_manage", request.user, catalog):
        return "manage"
    if has_object_permission("check_catalog_write", request.user, catalog):
        return "write"
    return "read"


def assert_same_catalog(catalog: Catalog, records, *, label: str) -> None:
    """Reject a write that would mix catalogs.

    Categories, feeds and entries are all scoped to one catalog, and nothing in
    the schema stops a model from pairing an entry in catalog A with a category
    from catalog B. Left unchecked that write succeeds and produces a
    classification no OPDS feed will ever render, so it is refused here with the
    offending ids named.
    """
    strays = [record for record in records if str(record.catalog_id) != str(catalog.pk)]
    if not strays:
        return

    raise ToolError(
        _("%(label)s %(ids)s belong to a different catalog than '%(catalog)s'. Both sides of a link must share one.")
        % {
            "label": label,
            "ids": ", ".join(str(record.pk) for record in strays[:10]),
            "catalog": catalog.title,
        }
    )


def assert_within_bulk_limit(name: str, identifiers) -> list:
    """Bound one bulk write, and reject the duplicate ids models like to send.

    Returns the ids de-duplicated but in the order given, so a per-record result
    list lines up with what the caller asked for.
    """
    if not identifiers:
        raise ToolError(_("`%(name)s` must contain at least one id.") % {"name": name})

    limit = settings.EVILFLOWERS_MCP_MAX_BULK_ITEMS
    if len(identifiers) > limit:
        raise ToolError(
            _("`%(name)s` holds %(count)d ids; at most %(limit)d may be written at once. Split the work into batches.")
            % {"name": name, "count": len(identifiers), "limit": limit}
        )

    return list(dict.fromkeys(identifiers))


def resolve_all(request, filter_class, model, identifiers, *, label: str, queryset=None):
    """Every named record, resolved through the *read* filter, or `ToolNotFound`.

    All-or-nothing on purpose. A bulk write that silently skipped the ids it
    could not resolve would report a success the caller cannot verify — and if
    the unresolvable ones are unreadable rather than absent, quietly dropping
    them hides a permission boundary the model needs to know about.
    """
    base = queryset if queryset is not None else model.objects.all()
    records = list(filter_class(QueryDict(mutable=True), queryset=base.filter(pk__in=identifiers), request=request).qs)

    found = {str(record.pk) for record in records}
    missing = [identifier for identifier in identifiers if identifier not in found]
    if missing:
        raise ToolNotFound(label, ", ".join(missing[:10]))

    by_id = {str(record.pk): record for record in records}
    return [by_id[identifier] for identifier in identifiers]


__all__ = [
    "access_mode",
    "assert_same_catalog",
    "assert_within_bulk_limit",
    "resolve_all",
    "resolve_catalog_for_management",
    "resolve_readable_catalog",
]
