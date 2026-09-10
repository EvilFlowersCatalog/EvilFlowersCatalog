"""Category management: browse the subject vocabulary, and curate it.

Mirrors `apps/api/views/categories.py` — `CategoryFilter` for scoping,
`CategoryForm` for validation, `check_catalog_manage` for every write, and the
same `(catalog, term)` uniqueness rule the model enforces.
"""

import logging

from django.db import transaction
from django.http import QueryDict
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.categories import CategoryFilter
from apps.api.forms.category import CategoryForm
from apps.core.models import Category
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolNotFound, ToolPermissionDenied, ToolValidationError
from apps.mcp.pagination import PAGINATION_ARGUMENTS, paginate, pagination_schema, read_pagination
from apps.mcp.projections import category as project_category
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import CATEGORY_SCHEMA, DELETION_OUTPUT_SCHEMA, UUID_SCHEMA, list_output, single_output
from apps.mcp.tools.common import resolve_catalog_for_management

logger = logging.getLogger("apps.mcp.audit")

CATEGORY_FIELDS = {
    "term": {
        "type": "string",
        "maxLength": 255,
        "description": "The machine-readable subject token, unique within the catalog (e.g. 'applied-informatics').",
    },
    "label": {"type": "string", "maxLength": 255, "description": "Human-readable name shown to readers."},
    "scheme": {
        "type": "string",
        "maxLength": 255,
        "description": "URI of the classification vocabulary this term belongs to, if any (e.g. a BISAC scheme URL).",
    },
}


def _category_payload(instance: Category) -> dict:
    return {"category": project_category(instance)}


def _readable_category(request, category_id: str) -> Category:
    """Resolve through `CategoryFilter` so read scoping matches `list_categories`."""
    category = (
        CategoryFilter(QueryDict(mutable=True), queryset=Category.objects.filter(pk=category_id), request=request)
        .qs.select_related("catalog")
        .first()
    )
    if category is None:
        raise ToolNotFound("Category", category_id)
    return category


def _manageable_category(request, category_id: str, action: str) -> Category:
    """Existence via the read scope, then the manage check — see `tools/common.py`."""
    category = _readable_category(request, category_id)
    if not has_object_permission("check_catalog_manage", request.user, category.catalog):
        raise ToolPermissionDenied(action, f"category '{category.term}'")
    return category


def _assert_term_free(catalog, term: str, *, exclude_pk=None) -> None:
    clashes = Category.objects.filter(catalog=catalog, term=term)
    if exclude_pk:
        clashes = clashes.exclude(pk=exclude_pk)
    if clashes.exists():
        raise ToolConflict(
            _("Catalog '%(catalog)s' already has a category with the term '%(term)s'.")
            % {"catalog": catalog.title, "term": term}
        )


def _validated_form(payload: dict) -> CategoryForm:
    form = CategoryForm(payload)
    form.fields["catalog_id"].required = True
    if not form.is_valid():
        raise ToolValidationError(form)
    return form


@registry.tool(
    name="get_category",
    title="Get category details",
    description="Fetch one category by id: its term, label and classification scheme.",
    input_schema={
        "type": "object",
        "properties": {"category_id": {**UUID_SCHEMA, "description": "Category id, from `list_categories`."}},
        "required": ["category_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("category", CATEGORY_SCHEMA),
)
def get_category(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("category_id",))
    return _category_payload(_readable_category(request, arguments.uuid("category_id", required=True)))


@registry.tool(
    name="create_category",
    title="Create a category",
    description=(
        "Add a subject category to a catalog. Requires `manage` access on that catalog. The "
        "`term` must be unique within the catalog — call `list_categories` first to avoid "
        "creating a near-duplicate of a term that already exists."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Catalog to create the category in."},
            "term": CATEGORY_FIELDS["term"],
            "label": CATEGORY_FIELDS["label"],
            "scheme": CATEGORY_FIELDS["scheme"],
        },
        "required": ["catalog_id", "term"],
        "additionalProperties": False,
    },
    output_schema=single_output("category", CATEGORY_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
@transaction.atomic
def create_category(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("catalog_id", "term", "label", "scheme"))

    catalog = resolve_catalog_for_management(
        request, arguments.uuid("catalog_id", required=True), "create a category in"
    )

    form = _validated_form(
        {
            "catalog_id": str(catalog.pk),
            "term": arguments.string("term", max_length=255),
            "label": arguments.string("label", max_length=255),
            "scheme": arguments.string("scheme", max_length=255),
        }
    )

    _assert_term_free(catalog, form.cleaned_data["term"])

    category = Category(creator=request.user)
    form.populate(category)
    category.save()

    logger.info(
        "mcp.write user=%s action=create_category category=%s catalog=%s", request.user.pk, category.pk, catalog.pk
    )
    return _category_payload(category)


@registry.tool(
    name="update_category",
    title="Update a category",
    description=(
        "Change a category's term, label or scheme. Requires `manage` access on the catalog. "
        "Only the arguments you pass are changed. Renaming a `term` re-labels every publication "
        "already filed under it — the publications keep their link to this category."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category_id": {**UUID_SCHEMA, "description": "Category to change."},
            "catalog_id": {
                **UUID_SCHEMA,
                "description": "Move the category to another catalog. Requires `manage` on both catalogs.",
            },
            "term": CATEGORY_FIELDS["term"],
            "label": CATEGORY_FIELDS["label"],
            "scheme": CATEGORY_FIELDS["scheme"],
        },
        "required": ["category_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("category", CATEGORY_SCHEMA),
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def update_category(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("category_id", "catalog_id", "term", "label", "scheme"))

    category = _manageable_category(request, arguments.uuid("category_id", required=True), "update")

    target_catalog_id = arguments.uuid("catalog_id")
    if target_catalog_id and str(target_catalog_id) != str(category.catalog_id):
        catalog = resolve_catalog_for_management(request, target_catalog_id, "move a category into")
    else:
        catalog = category.catalog

    form = _validated_form(
        {
            "catalog_id": str(catalog.pk),
            "term": arguments.string("term", max_length=255) or category.term,
            # `label` and `scheme` are nullable, so an explicitly-passed empty
            # string means "clear it" while omission means "leave it".
            "label": arguments.string("label", max_length=255) if "label" in raw_arguments else category.label,
            "scheme": arguments.string("scheme", max_length=255) if "scheme" in raw_arguments else category.scheme,
        }
    )

    _assert_term_free(catalog, form.cleaned_data["term"], exclude_pk=category.pk)

    form.populate(category)
    category.save()

    logger.info(
        "mcp.write user=%s action=update_category category=%s catalog=%s", request.user.pk, category.pk, catalog.pk
    )
    return _category_payload(category)


@registry.tool(
    name="delete_category",
    title="Delete a category",
    description=(
        "Permanently delete a category. Requires `manage` access on the catalog. Publications "
        "filed under it are **not** deleted — they simply lose this classification. Check "
        "`search_entries` with `category_ids` first to see how many are affected. This cannot be "
        "undone."
    ),
    input_schema={
        "type": "object",
        "properties": {"category_id": {**UUID_SCHEMA, "description": "Category to delete."}},
        "required": ["category_id"],
        "additionalProperties": False,
    },
    output_schema={
        **DELETION_OUTPUT_SCHEMA,
        "properties": {
            **DELETION_OUTPUT_SCHEMA["properties"],
            "unfiled_entries": {
                "type": "integer",
                "description": "How many publications lost this classification.",
            },
        },
    },
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def delete_category(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("category_id",))
    category = _manageable_category(request, arguments.uuid("category_id", required=True), "delete")

    identifier, term, catalog_id = str(category.pk), category.term, category.catalog_id
    # Counted before the delete so the result can tell the model — and through
    # it the human — the actual blast radius of what just happened.
    affected = category.entries.count()
    category.delete()

    logger.info(
        "mcp.write user=%s action=delete_category category=%s catalog=%s unfiled=%s",
        request.user.pk,
        identifier,
        catalog_id,
        affected,
    )
    return {"deleted": True, "id": identifier, "title": term, "unfiled_entries": affected}


@registry.tool(
    name="list_categories",
    title="List categories",
    description=(
        "Browse the subject vocabulary in use. Returns category terms with their human-readable "
        "labels and ids; pass the ids to `search_entries` via `category_ids`. Useful for "
        "answering 'what subjects does this library cover?' and for checking whether a term "
        "already exists before creating one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search across the category term and its label."},
            "catalog_id": {**UUID_SCHEMA, "description": "Restrict to one catalog."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(CATEGORY_SCHEMA),
)
def list_categories(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("query", "catalog_id", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)

    params = QueryDict(mutable=True)
    for key, value in (("query", arguments.string("query")), ("catalog_id", arguments.uuid("catalog_id"))):
        if value:
            params[key] = value

    categories = (
        CategoryFilter(params, queryset=Category.objects.all(), request=request).qs.order_by("term").distinct()
    )
    page = paginate(categories, requested_page)

    return {"items": [project_category(item) for item in page.items], "metadata": page.metadata()}
