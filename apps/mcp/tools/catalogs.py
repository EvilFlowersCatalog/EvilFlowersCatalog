"""Catalogs and authors: the collections a search filters by, and who wrote what.

A model that guesses `author="Lovelace"` may or may not hit; one that resolves
the name to an id first always does. These tools exist so a search can be
precise instead of hopeful.

The catalog *management* tools live here too, and they mirror
`apps/api/views/catalogs.py`: `CatalogForm` for validation, `CatalogService` for
population, `core.add_catalog` for creation and `check_catalog_manage` for every
subsequent change.

Note what `core.add_catalog` means in practice. `Catalog.Meta` sets
`default_permissions = ()`, so that permission row does not exist and
`has_perm` returns True only for a superuser. Creating a catalog is therefore a
superuser action on both surfaces — deliberately mirrored rather than relaxed,
because a catalog is a tenant boundary and handing an agent the ability to mint
tenants is not a decision this app should make quietly.

Categories live in `tools/categories.py`, next to their own management tools.
"""

import logging

from django.db import transaction
from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.api.filters.authors import AuthorFilter
from apps.api.filters.catalogs import CatalogFilter
from apps.api.forms.catalogs import CatalogForm
from apps.api.services.catalog import CatalogService
from apps.core.models import Author, Catalog, UserCatalog
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolError, ToolPermissionDenied, ToolValidationError
from apps.mcp.pagination import PAGINATION_ARGUMENTS, paginate, pagination_schema, read_pagination
from apps.mcp.projections import author, catalog, catalog_detail
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import (
    AUTHOR_SCHEMA,
    CATALOG_DETAIL_SCHEMA,
    CATALOG_SCHEMA,
    DELETION_OUTPUT_SCHEMA,
    UUID_SCHEMA,
    list_output,
    single_output,
)
from apps.mcp.tools.common import access_mode, resolve_catalog_for_management, resolve_readable_catalog

logger = logging.getLogger("apps.mcp.audit")

CATALOG_FIELDS = {
    "title": {
        "type": "string",
        "maxLength": 100,
        "description": "Human-readable collection name, unique among the catalogs you created.",
    },
    "url_name": {
        "type": "string",
        "maxLength": 50,
        "description": (
            "URL slug, unique across the whole deployment — it appears in every OPDS and portal "
            "URL for this catalog. Lower-case letters, digits and hyphens."
        ),
    },
    "is_public": {
        "type": "boolean",
        "description": (
            "True makes every publication in the catalog readable by anonymous callers, here and "
            "on the public OPDS feeds. Changing it re-scopes the whole collection at once."
        ),
    },
}


def _params(pairs) -> QueryDict:
    params = QueryDict(mutable=True)
    for key, value in pairs:
        if value:
            params[key] = value
    return params


def _assert_slug_free(url_name: str, *, exclude_pk=None) -> None:
    """`Catalog.url_name` is unique deployment-wide, across catalogs you cannot see.

    So this check deliberately queries unfiltered. It leaks one bit — "that slug
    is taken" — which is unavoidable: the database will refuse the write anyway,
    and a 500 tells the model less while leaking the same fact.
    """
    clashes = Catalog.objects.filter(url_name=url_name)
    if exclude_pk:
        clashes = clashes.exclude(pk=exclude_pk)
    if clashes.exists():
        raise ToolConflict(
            _("The url_name '%(url_name)s' is already taken by another catalog.") % {"url_name": url_name}
        )


def _assert_title_free(creator_id, title: str, *, exclude_pk=None) -> None:
    """`Catalog.Meta.unique_together = ("creator_id", "title")`.

    Unchecked by the REST endpoint, which only guards `url_name`, so a repeated
    title reaches the database and surfaces as a 500.
    """
    clashes = Catalog.objects.filter(creator_id=creator_id, title=title)
    if exclude_pk:
        clashes = clashes.exclude(pk=exclude_pk)
    if clashes.exists():
        raise ToolConflict(_("You already own a catalog titled '%(title)s'.") % {"title": title})


def _validated_form(payload: dict) -> CatalogForm:
    form = CatalogForm(payload)
    if not form.is_valid():
        raise ToolValidationError(form)
    return form


@registry.tool(
    name="list_catalogs",
    title="List catalogs",
    description=(
        "List the catalogs (collections of publications) this credential may read. Each carries "
        "an `access` level — only 'manage' unlocks the curation tools — so this is the right "
        "first call before any organisational work. Use a catalog id to scope `search_entries` "
        "to one collection, or to create feeds and categories in it. An unauthenticated session "
        "sees public catalogs only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Substring match on the catalog title."},
            "manageable_only": {
                "type": "boolean",
                "description": "True returns only catalogs this credential may curate.",
            },
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(
        {
            **CATALOG_SCHEMA,
            "properties": {
                **CATALOG_SCHEMA["properties"],
                "access": {
                    "type": "string",
                    "enum": ["read", "write", "manage"],
                },
            },
        }
    ),
)
def list_catalogs(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("title", "manageable_only", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)
    params = _params((("title", arguments.string("title")),))

    catalogs = CatalogFilter(params, queryset=Catalog.objects.all(), request=request).qs.order_by("title")

    # One query for the whole page rather than `check_catalog_manage` per row,
    # which would be a query each. A superuser manages everything by definition,
    # so there is nothing to look up for them.
    if request.user.is_authenticated and not request.user.is_superuser:
        modes = dict(
            UserCatalog.objects.filter(user=request.user).values_list("catalog_id", "mode"),
        )
    else:
        modes = {}

    if arguments.boolean("manageable_only"):
        if request.user.is_superuser:
            pass
        elif not request.user.is_authenticated:
            catalogs = catalogs.none()
        else:
            catalogs = catalogs.filter(pk__in=[pk for pk, mode in modes.items() if mode == UserCatalog.Mode.MANAGE])

    page = paginate(catalogs, requested_page)

    def _access(instance: Catalog) -> str:
        if request.user.is_superuser:
            return "manage"
        mode = modes.get(instance.pk)
        return mode if mode in ("manage", "write") else "read"

    return {
        "items": [{**catalog(item), "access": _access(item)} for item in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="get_catalog",
    title="Get catalog details",
    description=(
        "Fetch one catalog: its slug, visibility, how much it holds, and what the calling "
        "credential may do with it. Call this before planning curation work — `access` tells you "
        "whether the management tools will accept anything at all."
    ),
    input_schema={
        "type": "object",
        "properties": {"catalog_id": {**UUID_SCHEMA, "description": "Catalog id, from `list_catalogs`."}},
        "required": ["catalog_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("catalog", CATALOG_DETAIL_SCHEMA),
)
def get_catalog(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("catalog_id",))
    instance = resolve_readable_catalog(request, arguments.uuid("catalog_id", required=True))

    return {"catalog": catalog_detail(instance, access_mode(request, instance))}


@registry.tool(
    name="create_catalog",
    title="Create a catalog",
    description=(
        "Create a new catalog — a top-level collection with its own publications, feeds, "
        "categories and access list. **Restricted to superusers**: a catalog is a tenant "
        "boundary, so ordinary `manage` access on an existing catalog does not confer it. The "
        "caller is granted `manage` on whatever they create. `url_name` must be unique across "
        "the entire deployment."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": CATALOG_FIELDS["title"],
            "url_name": CATALOG_FIELDS["url_name"],
            "is_public": CATALOG_FIELDS["is_public"],
        },
        "required": ["title", "url_name"],
        "additionalProperties": False,
    },
    output_schema=single_output("catalog", CATALOG_DETAIL_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
@transaction.atomic
def create_catalog(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("title", "url_name", "is_public"))

    # Same gate as `POST /api/v1/catalogs`. With `default_permissions = ()` on
    # the model this is a superuser check in all but name.
    if not request.user.has_perm("core.add_catalog"):
        raise ToolError(
            _(
                "Creating a catalog is restricted to administrators. `manage` access on an "
                "existing catalog does not confer it — ask an administrator to create the "
                "catalog, then curate it with `create_feed` and `create_category`."
            )
        )

    form = _validated_form(
        {
            "title": arguments.string("title", max_length=100),
            "url_name": arguments.string("url_name", max_length=50),
            "is_public": arguments.boolean("is_public", default=False),
        }
    )

    _assert_slug_free(form.cleaned_data["url_name"])
    _assert_title_free(request.user.pk, form.cleaned_data["title"])

    instance = CatalogService().populate(catalog=Catalog(creator=request.user), form=form)

    # Mirrors the REST endpoint: the creator would otherwise be locked out of
    # the catalog they just made.
    if not instance.users.contains(request.user):
        UserCatalog.objects.create(catalog=instance, user=request.user, mode=UserCatalog.Mode.MANAGE)

    logger.info("mcp.write user=%s action=create_catalog catalog=%s", request.user.pk, instance.pk)
    return {"catalog": catalog_detail(instance, "manage")}


@registry.tool(
    name="update_catalog",
    title="Update a catalog",
    description=(
        "Rename a catalog, change its slug, or switch it between public and private. Requires "
        "`manage` access. Only the arguments you pass are changed. Setting `is_public` re-scopes "
        "every publication in the collection at once — confirm with the user before flipping it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Catalog to change."},
            "title": CATALOG_FIELDS["title"],
            "url_name": CATALOG_FIELDS["url_name"],
            "is_public": CATALOG_FIELDS["is_public"],
        },
        "required": ["catalog_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("catalog", CATALOG_DETAIL_SCHEMA),
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def update_catalog(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("catalog_id", "title", "url_name", "is_public"))

    instance = resolve_catalog_for_management(request, arguments.uuid("catalog_id", required=True), "update")

    form = _validated_form(
        {
            "title": arguments.string("title", max_length=100) or instance.title,
            "url_name": arguments.string("url_name", max_length=50) or instance.url_name,
            "is_public": arguments.boolean("is_public", default=instance.is_public),
        }
    )

    _assert_slug_free(form.cleaned_data["url_name"], exclude_pk=instance.pk)
    _assert_title_free(instance.creator_id, form.cleaned_data["title"], exclude_pk=instance.pk)

    # `CatalogService.populate` rewrites the access list when the form carries a
    # `users` key. This tool never offers one, so membership is untouched —
    # managing who may read a catalog stays a portal action.
    instance = CatalogService().populate(catalog=instance, form=form)

    logger.info("mcp.write user=%s action=update_catalog catalog=%s", request.user.pk, instance.pk)
    return {"catalog": catalog_detail(instance, access_mode(request, instance))}


@registry.tool(
    name="delete_catalog",
    title="Delete a catalog",
    description=(
        "Permanently delete a catalog **and everything inside it** — every publication, file, "
        "feed, category, annotation and loan record. This is the most destructive tool on the "
        "server and it cannot be undone. To guard against a mistaken call you must pass "
        "`confirm_title` matching the catalog's exact title; call `get_catalog` first, show the "
        "user what will be destroyed, and only then confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Catalog to delete."},
            "confirm_title": {
                "type": "string",
                "description": "The catalog's exact current title, repeated back as confirmation.",
            },
        },
        "required": ["catalog_id", "confirm_title"],
        "additionalProperties": False,
    },
    output_schema={
        **DELETION_OUTPUT_SCHEMA,
        "properties": {
            **DELETION_OUTPUT_SCHEMA["properties"],
            "deleted_entries": {"type": "integer", "description": "How many publications were destroyed with it."},
        },
    },
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def delete_catalog(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("catalog_id", "confirm_title"))

    instance = resolve_catalog_for_management(request, arguments.uuid("catalog_id", required=True), "delete")

    # A `destructiveHint` annotation asks the *client* to prompt, which is the
    # client's choice to honour. Losing an entire library to an unprompted call
    # is bad enough to warrant a check the server can actually enforce.
    confirmation = arguments.string("confirm_title", max_length=100)
    if confirmation != instance.title:
        raise ToolError(
            _(
                "`confirm_title` does not match. To delete this catalog pass its exact title, "
                "'%(title)s'. Nothing has been deleted."
            )
            % {"title": instance.title}
        )

    identifier, title = str(instance.pk), instance.title
    entries = instance.entries.count()
    instance.delete()

    logger.info("mcp.write user=%s action=delete_catalog catalog=%s entries=%s", request.user.pk, identifier, entries)
    return {"deleted": True, "id": identifier, "title": title, "deleted_entries": entries}


@registry.tool(
    name="list_authors",
    title="List authors",
    description=(
        "Look up authors by name to obtain their ids, then pass those to `search_entries` via "
        "`author_ids` for an exact match. Prefer this over guessing at the free-text `author` "
        "filter when the name spelling is uncertain."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search across first name, surname and full name."},
            "catalog_id": {**UUID_SCHEMA, "description": "Restrict to one catalog."},
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(AUTHOR_SCHEMA),
)
def list_authors(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("query", "catalog_id", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)
    params = _params(
        (
            ("query", arguments.string("query")),
            ("catalog_id", arguments.uuid("catalog_id")),
        )
    )

    authors = AuthorFilter(params, queryset=Author.objects.all(), request=request).qs.order_by("surname", "name")
    page = paginate(authors, requested_page)

    return {"items": [author(item) for item in page.items], "metadata": page.metadata()}
