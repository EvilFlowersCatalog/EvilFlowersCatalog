"""Feed management: browse the navigation tree, and curate it.

Feeds are the catalog's organisational spine — navigation feeds form a tree,
acquisition feeds hold entries. These tools mirror `apps/api/views/feeds.py`:
the same `FeedFilter` for scoping, the same `FeedForm` for validation, and the
same `check_catalog_manage` / `check_catalog_read` checkers.

Two places where they are deliberately **stricter** than the REST endpoint, both
noted inline: moving a feed between catalogs is permission-checked on both
sides, and the `(catalog, title)` uniqueness constraint is checked rather than
left to surface as an IntegrityError.
"""

import logging

from django.db import transaction
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.feeds import FeedFilter
from apps.api.forms.feeds import FeedForm
from apps.core.models import Catalog, Feed
from apps.mcp.arguments import Arguments
from apps.mcp.errors import ToolConflict, ToolNotFound, ToolPermissionDenied, ToolValidationError
from apps.mcp.pagination import PAGINATION_ARGUMENTS, paginate, pagination_schema, read_pagination
from apps.mcp.projections import feed as project_feed
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import FEED_SCHEMA, DELETION_OUTPUT_SCHEMA, UUID_SCHEMA, list_output, single_output
from apps.mcp.tools.common import resolve_catalog_for_management

logger = logging.getLogger("apps.mcp.audit")

FEED_KINDS = tuple(kind.value for kind in Feed.FeedKind)

# Shared between create and update so the two cannot describe the same field
# differently — a model reading both should see one vocabulary.
FEED_FIELDS = {
    "title": {
        "type": "string",
        "maxLength": 100,
        "description": "Human-readable feed name, unique within the catalog.",
    },
    "url_name": {
        "type": "string",
        "maxLength": 50,
        "description": "URL slug, unique within the catalog. Lower-case letters, digits and hyphens.",
    },
    "kind": {
        "type": "string",
        "enum": list(FEED_KINDS),
        "description": (
            "'navigation' groups other feeds into a tree and may not hold entries directly; "
            "'acquisition' holds publications."
        ),
    },
    "content": {"type": "string", "description": "Description shown to readers browsing the feed."},
    "per_page": {"type": "integer", "minimum": 1, "description": "Publications per OPDS page. Omit for the default."},
    "entry_ids": {
        "type": "array",
        "items": UUID_SCHEMA,
        "description": "Publications in this feed. Acquisition feeds only. Replaces the current set.",
    },
    "parent_ids": {
        "type": "array",
        "items": UUID_SCHEMA,
        "description": "Navigation feeds this one hangs under. Replaces the current set.",
    },
}


def _feed_payload(instance: Feed, request) -> dict:
    return {"feed": project_feed(instance, request)}


def _readable_feed(request, feed_id: str) -> Feed:
    """Resolve through `FeedFilter` so read scoping matches `list_feeds` exactly."""
    from django.http import QueryDict

    params = QueryDict(mutable=True)
    feed = (
        FeedFilter(params, queryset=Feed.objects.filter(pk=feed_id), request=request)
        .qs.select_related("catalog")
        .first()
    )
    if feed is None:
        raise ToolNotFound("Feed", feed_id)
    return feed


def _manageable_feed(request, feed_id: str, action: str) -> Feed:
    """Resolve a feed the caller may manage.

    Existence is resolved with the *read* scope first so a caller who can see a
    feed but not manage it gets "insufficient permissions", while a caller who
    cannot see it at all gets "not found" — the latter must not become an
    existence oracle for other tenants' catalogs.
    """
    feed = _readable_feed(request, feed_id)
    if not has_object_permission("check_catalog_manage", request.user, feed.catalog):
        raise ToolPermissionDenied(action, f"feed '{feed.title}'")
    return feed


def _assert_unique(catalog, title: str, url_name: str, *, exclude_pk=None) -> None:
    """Both halves of `Feed.Meta.unique_together`.

    The REST endpoint checks `url_name` only, so a duplicate title reaches the
    database and surfaces as a 500. Checking both here turns it into a message
    the model can act on.
    """
    clashes = Feed.objects.filter(catalog=catalog)
    if exclude_pk:
        clashes = clashes.exclude(pk=exclude_pk)

    if clashes.filter(url_name=url_name).exists():
        raise ToolConflict(
            _("A feed with url_name '%(url_name)s' already exists in catalog '%(catalog)s'.")
            % {"url_name": url_name, "catalog": catalog.title}
        )
    if clashes.filter(title=title).exists():
        raise ToolConflict(
            _("A feed titled '%(title)s' already exists in catalog '%(catalog)s'.")
            % {"title": title, "catalog": catalog.title}
        )


def _validated_form(payload: dict, *, exclude_pk=None) -> FeedForm:
    form = FeedForm(payload)
    if exclude_pk is not None:
        # A feed may not be its own parent.
        form.fields["parents"].queryset = form.fields["parents"].queryset.exclude(pk=exclude_pk)
    if not form.is_valid():
        raise ToolValidationError(form)
    return form


@registry.tool(
    name="list_feeds",
    title="List feeds",
    description=(
        "List the feeds (curated collections and navigation nodes) visible to this credential. "
        'Filter by catalog, kind or parent to walk the navigation tree. Use `parent_id: "null"` '
        "for top-level feeds."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Restrict to one catalog."},
            "title": {"type": "string", "description": "Substring match on the feed title."},
            "kind": {"type": "string", "enum": list(FEED_KINDS), "description": "Restrict to one feed kind."},
            "parent_id": {
                "type": "string",
                "description": "A parent feed's UUID, or the literal 'null' for top-level feeds.",
            },
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(FEED_SCHEMA),
)
def list_feeds(request, raw_arguments: dict) -> dict:
    from django.db.models import Count
    from django.http import QueryDict

    arguments = Arguments(raw_arguments, allowed=("catalog_id", "title", "kind", "parent_id", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)

    params = QueryDict(mutable=True)
    parent_id = arguments.string("parent_id")
    for key, value in (
        ("catalog_id", arguments.uuid("catalog_id")),
        ("title", arguments.string("title")),
        ("kind", arguments.enum("kind", FEED_KINDS)),
        # `FeedFilter` accepts the literal 'null' here; anything else must be a
        # UUID, which the filter validates and reports itself.
        ("parent_id", parent_id),
    ):
        if value:
            params[key] = value

    feeds = (
        FeedFilter(params, queryset=Feed.objects.all(), request=request)
        .qs.select_related("catalog")
        .annotate(entry_count=Count("entries", distinct=True))
        .order_by("title")
        .distinct()
    )
    page = paginate(feeds, requested_page)

    return {
        "items": [project_feed(item, request) for item in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="get_feed",
    title="Get feed details",
    description="Fetch one feed: its metadata, position in the navigation tree, and OPDS URL.",
    input_schema={
        "type": "object",
        "properties": {"feed_id": {**UUID_SCHEMA, "description": "Feed id, as returned by `list_feeds`."}},
        "required": ["feed_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("feed", FEED_SCHEMA),
)
def get_feed(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("feed_id",))
    return _feed_payload(_readable_feed(request, arguments.uuid("feed_id", required=True)), request)


@registry.tool(
    name="create_feed",
    title="Create a feed",
    description=(
        "Create a feed in a catalog. Requires `manage` access on that catalog. A 'navigation' "
        "feed groups other feeds and cannot hold entries; an 'acquisition' feed holds "
        "publications. `title` and `url_name` must each be unique within the catalog."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {**UUID_SCHEMA, "description": "Catalog to create the feed in."},
            "title": FEED_FIELDS["title"],
            "url_name": FEED_FIELDS["url_name"],
            "kind": FEED_FIELDS["kind"],
            "content": FEED_FIELDS["content"],
            "per_page": FEED_FIELDS["per_page"],
            "entry_ids": FEED_FIELDS["entry_ids"],
            "parent_ids": FEED_FIELDS["parent_ids"],
        },
        "required": ["catalog_id", "title", "url_name", "kind", "content"],
        "additionalProperties": False,
    },
    output_schema=single_output("feed", FEED_SCHEMA),
    access=ToolAccess.WRITE,
    idempotent=False,
)
@transaction.atomic
def create_feed(request, raw_arguments: dict) -> dict:
    arguments = Arguments(
        raw_arguments,
        allowed=("catalog_id", "title", "url_name", "kind", "content", "per_page", "entry_ids", "parent_ids"),
    )

    catalog = resolve_catalog_for_management(request, arguments.uuid("catalog_id", required=True), "create a feed in")

    form = _validated_form(
        {
            "catalog_id": str(catalog.pk),
            "title": arguments.string("title"),
            "url_name": arguments.string("url_name"),
            "kind": arguments.enum("kind", FEED_KINDS),
            "content": arguments.string("content", max_length=20_000),
            "per_page": (
                arguments.integer("per_page", default=None, minimum=1, maximum=1000)
                if "per_page" in raw_arguments
                else None
            ),
            "entries": arguments.uuid_sequence("entry_ids"),
            "parents": arguments.uuid_sequence("parent_ids"),
        }
    )

    _assert_unique(catalog, form.cleaned_data["title"], form.cleaned_data["url_name"])

    feed = Feed(creator=request.user)
    form.populate(feed)
    # `FeedForm` does not carry `source`, so the REST endpoint stores an empty
    # string in a `choices` column. `RELATION` is the only member of
    # `Feed.FeedSource`, so set it rather than persist an invalid value.
    feed.source = Feed.FeedSource.RELATION
    feed.save()

    if form.cleaned_data.get("entries"):
        feed.entries.add(*form.cleaned_data["entries"])
    if form.cleaned_data.get("parents"):
        feed.parents.add(*form.cleaned_data["parents"])

    logger.info("mcp.write user=%s action=create_feed feed=%s catalog=%s", request.user.pk, feed.pk, catalog.pk)
    return _feed_payload(feed, request)


@registry.tool(
    name="update_feed",
    title="Update a feed",
    description=(
        "Change a feed. Requires `manage` access on the catalog. Only the arguments you pass are "
        "changed — everything else keeps its current value. Passing `entry_ids` or `parent_ids` "
        "**replaces** that whole set rather than appending to it, so read the feed first if you "
        "mean to add one item."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "feed_id": {**UUID_SCHEMA, "description": "Feed to change."},
            "catalog_id": {
                **UUID_SCHEMA,
                "description": "Move the feed to another catalog. Requires `manage` on both catalogs.",
            },
            "title": FEED_FIELDS["title"],
            "url_name": FEED_FIELDS["url_name"],
            "kind": FEED_FIELDS["kind"],
            "content": FEED_FIELDS["content"],
            "per_page": FEED_FIELDS["per_page"],
            "entry_ids": FEED_FIELDS["entry_ids"],
            "parent_ids": FEED_FIELDS["parent_ids"],
        },
        "required": ["feed_id"],
        "additionalProperties": False,
    },
    output_schema=single_output("feed", FEED_SCHEMA),
    access=ToolAccess.WRITE,
    destructive=True,
)
@transaction.atomic
def update_feed(request, raw_arguments: dict) -> dict:
    arguments = Arguments(
        raw_arguments,
        allowed=(
            "feed_id",
            "catalog_id",
            "title",
            "url_name",
            "kind",
            "content",
            "per_page",
            "entry_ids",
            "parent_ids",
        ),
    )

    feed = _manageable_feed(request, arguments.uuid("feed_id", required=True), "update")

    target_catalog_id = arguments.uuid("catalog_id")
    if target_catalog_id and str(target_catalog_id) != str(feed.catalog_id):
        # The REST endpoint checks `manage` on the feed's *current* catalog and
        # then lets the form move it anywhere, so a manager of A can push a feed
        # into B. Check the destination too.
        catalog = resolve_catalog_for_management(request, target_catalog_id, "move a feed into")
    else:
        catalog = feed.catalog

    # Merge over current values so the tool behaves like PATCH while still
    # validating through the same all-fields-required form as REST.
    payload = {
        "catalog_id": str(catalog.pk),
        "title": arguments.string("title") or feed.title,
        "url_name": arguments.string("url_name") or feed.url_name,
        "kind": arguments.enum("kind", FEED_KINDS) or feed.kind,
        "content": arguments.string("content", max_length=20_000) or feed.content,
        "per_page": arguments.integer("per_page", default=feed.per_page, minimum=1, maximum=1000),
    }

    replaces_entries = "entry_ids" in raw_arguments
    replaces_parents = "parent_ids" in raw_arguments
    if replaces_entries:
        payload["entries"] = arguments.uuid_sequence("entry_ids") or []
    if replaces_parents:
        payload["parents"] = arguments.uuid_sequence("parent_ids") or []

    form = _validated_form(payload, exclude_pk=feed.pk)
    _assert_unique(catalog, form.cleaned_data["title"], form.cleaned_data["url_name"], exclude_pk=feed.pk)

    form.populate(feed)
    feed.save()

    if replaces_entries:
        if feed.kind == Feed.FeedKind.NAVIGATION:
            # `FeedForm.clean` already rejects entries on a navigation feed, but
            # only when they are non-empty; clear defensively so a kind change
            # cannot strand rows behind a feed that may not show them.
            feed.entries.clear()
        else:
            feed.entries.set(form.cleaned_data.get("entries") or [])
    if replaces_parents:
        feed.parents.set(form.cleaned_data.get("parents") or [])

    logger.info("mcp.write user=%s action=update_feed feed=%s catalog=%s", request.user.pk, feed.pk, catalog.pk)
    return _feed_payload(feed, request)


@registry.tool(
    name="delete_feed",
    title="Delete a feed",
    description=(
        "Permanently delete a feed. Requires `manage` access on the catalog. The publications "
        "inside are **not** deleted — only the feed that grouped them. Child feeds are detached, "
        "not removed. This cannot be undone."
    ),
    input_schema={
        "type": "object",
        "properties": {"feed_id": {**UUID_SCHEMA, "description": "Feed to delete."}},
        "required": ["feed_id"],
        "additionalProperties": False,
    },
    output_schema=DELETION_OUTPUT_SCHEMA,
    access=ToolAccess.WRITE,
    destructive=True,
)
def delete_feed(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("feed_id",))
    feed = _manageable_feed(request, arguments.uuid("feed_id", required=True), "delete")

    identifier, title, catalog_id = str(feed.pk), feed.title, feed.catalog_id
    feed.delete()

    logger.info("mcp.write user=%s action=delete_feed feed=%s catalog=%s", request.user.pk, identifier, catalog_id)
    return {"deleted": True, "id": identifier, "title": title}
