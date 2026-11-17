"""IP-035: the base `ModelAdmin` of the console and the `register` decorator apps use.

The base class makes a model safe to show before anyone tunes it:

* **read-only unless the admin says otherwise** (`editable`, `deletable`). Most tables are written by
  services that keep invariants a bare form would not (search vectors, OAI records, author links, LCP
  state), so the console changes only what an admin opts in to. `EVILFLOWERS_ADMIN_READ_ONLY` turns
  every write off for a whole deployment;
* **gated by its module**: a module switched off hides its models and answers their URLs with 403,
  without a restart (`apps.console.modules`);
* **cheap on big tables** (`big = True`): no full count, an estimated row count from the planner
  statistics when nothing filters the list, no facet counts;
* **no vectors in forms or lists**: pgvector and full-text vector columns are deferred and excluded,
  shown only as a summary (dimensions, norm);
* **no unbounded selects**: every foreign key is a raw-id field unless the admin names autocomplete
  fields, so a form never renders 400 000 paragraphs into a `<select>`.
"""

import math
from typing import Iterable, List, Optional, Tuple

from django.conf import settings
from django.contrib import admin
from django.contrib.postgres.search import SearchVectorField
from django.core.paginator import Paginator
from django.db import connection, models
from django.db.models import QuerySet
from django.utils.functional import cached_property
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _

try:  # pgvector is a runtime dependency; the guard keeps the module importable in stripped test envs.
    from pgvector.django import HalfVectorField, SparseVectorField, VectorField

    VECTOR_FIELDS: Tuple[type, ...] = (VectorField, HalfVectorField, SparseVectorField)
except ImportError:  # pragma: no cover
    VECTOR_FIELDS = ()

#: Above this many (estimated) rows an unfiltered list shows the planner's estimate instead of counting.
ESTIMATE_THRESHOLD = 20_000

LIST_SKIP = (models.TextField, models.JSONField, models.BinaryField, models.FileField, SearchVectorField)
SEARCHABLE_NAMES = ("title", "name", "username", "surname", "key", "slug", "url_name", "term", "code", "kind")


def is_vector(field) -> bool:
    return bool(VECTOR_FIELDS) and isinstance(field, VECTOR_FIELDS)


def heavy_fields(model) -> List[str]:
    """Columns never worth loading for a list or a form: embeddings and full-text vectors."""
    return [
        field.name for field in model._meta.concrete_fields if is_vector(field) or isinstance(field, SearchVectorField)
    ]


def estimated_count(model) -> int:
    """The planner's row estimate for the model's table (`pg_class.reltuples`), -1 when unknown."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)", [model._meta.db_table])
        row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else -1


class EstimatedCountPaginator(Paginator):
    """Counts filtered lists exactly; an unfiltered list of a big table uses the planner's estimate."""

    @cached_property
    def count(self):
        queryset = self.object_list
        if isinstance(queryset, QuerySet) and not queryset.query.where:
            estimate = estimated_count(queryset.model)
            if estimate >= ESTIMATE_THRESHOLD:
                return estimate
        return super().count


def read_only_deployment() -> bool:
    return bool(getattr(settings, "EVILFLOWERS_ADMIN_READ_ONLY", False))


class ConsoleMixin:
    """Module gating, the read-only default and the safety defaults; mixed into any `ModelAdmin`."""

    #: Set by `register`: the key of the module the model belongs to.
    console_module: Optional[str] = None
    #: May the console add and change rows?
    editable: bool = False
    #: May the console delete rows?
    deletable: bool = False
    #: A table with hundreds of thousands of rows: estimated counts, no facets, short pages.
    big: bool = False
    #: Columns the data grid (`/admin/console/grid/…`) shows; `None` derives them like `list_display`.
    grid_fields: Optional[Iterable[str]] = None
    #: May the console add rows? `None` follows `editable`; set False where rows are only made elsewhere.
    addable: Optional[bool] = None
    #: Offer a card ("gallery") view of the changelist, built from `card()`.
    gallery: bool = False

    # --- record page and gallery (apps.console.records) --------------------------------------------------

    def record(self, request, obj):
        """The record page of `obj`: hero, figures, related rows, lists. Extend in an admin, keep `super()`."""
        from apps.console.records import default_record

        return default_record(self, request, obj)

    def card(self, request, obj):
        """One gallery tile of `obj`."""
        from apps.console.records import Card, change_url, initials

        return Card(title=str(obj), url=change_url(obj), initials=initials(str(obj)))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        response = super().change_view(request, object_id, form_url, extra_context)
        original = getattr(response, "context_data", None) and response.context_data.get("original")
        if original is not None and not response.context_data.get("is_popup"):
            response.context_data["console_record"] = self.record(request, original)
        return response

    def add_view(self, request, form_url="", extra_context=None):
        from apps.console.records import new_record

        response = super().add_view(request, form_url, extra_context)
        if getattr(response, "context_data", None) is not None and not response.context_data.get("is_popup"):
            response.context_data["console_record"] = new_record(self)
        return response

    def changelist_view(self, request, extra_context=None):
        view = None
        if self.gallery:
            # `view` is the console's own parameter: off the querystring before ChangeList reads it as a lookup.
            if "view" in request.GET:
                query = request.GET.copy()
                view = query.pop("view")[0]
                request.GET = query
                request.session[f"console:view:{self.model._meta.label}"] = view
            else:
                view = request.session.get(f"console:view:{self.model._meta.label}")
        response = super().changelist_view(request, extra_context)
        context = getattr(response, "context_data", None)
        if self.gallery and context is not None and "cl" in context:
            context["console_gallery_available"] = True
            context["console_view"] = "gallery" if view == "gallery" else "list"
            if view == "gallery":
                context["console_cards"] = [self.card(request, obj) for obj in context["cl"].result_list]
        return response

    # --- module gating ---------------------------------------------------------------------------------

    def module_enabled(self, request=None) -> bool:
        """Whether this admin's module is on. Memoised on the request: the index and the sidebar ask every
        admin several times, and each answer would otherwise be a cache (Redis) round trip."""
        from apps.console.modules import states

        if self.console_module is None:
            return False
        if request is None:
            return states()[self.console_module]["enabled"]
        memo = getattr(request, "_console_module_states", None)
        if memo is None:
            memo = {key: state["enabled"] for key, state in states().items()}
            request._console_module_states = memo
        return memo.get(self.console_module, False)

    def writes_allowed(self, request) -> bool:
        return not read_only_deployment() and self.module_enabled(request)

    def has_module_permission(self, request):
        return self.module_enabled(request) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self.module_enabled(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request, *args, **kwargs):
        addable = self.editable if self.addable is None else self.addable
        return addable and self.writes_allowed(request) and super().has_add_permission(request, *args, **kwargs)

    def has_change_permission(self, request, obj=None):
        return self.editable and self.writes_allowed(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self.deletable and self.writes_allowed(request) and super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # Actions write (moderate, accept, acknowledge, revoke): none at all in a read-only deployment.
        if read_only_deployment():
            return {}
        return super().get_actions(request)

    def save_model(self, request, obj, form, change):
        # Rows with a `creator` record who made them, as the REST API does.
        if not change and hasattr(obj, "creator_id") and obj.creator_id is None:
            obj.creator = request.user
        # An optional form field (see `formfield_for_dbfield`) left empty on a NOT NULL column takes the
        # column's default (an entry's `config`, `origin`) instead of failing on the constraint.
        for field in self.model._meta.concrete_fields:
            if not field.null and field.has_default() and getattr(obj, field.attname, None) is None:
                setattr(obj, field.attname, field.get_default())
        super().save_model(request, obj, form, change)

    # --- safe defaults ---------------------------------------------------------------------------------

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        heavy = heavy_fields(self.model)
        return queryset.defer(*heavy) if heavy else queryset

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or ())
        return exclude + [name for name in heavy_fields(self.model) if name not in exclude]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if any(is_vector(field) for field in self.model._meta.concrete_fields) and "vector_summary" not in readonly:
            readonly.append("vector_summary")
        # `creator` is filled in from the request on add (`save_model`), never typed; the primary key and the
        # timestamps come from their defaults (`BaseModel`: uuid4, `Now()`), so a form must not ask for them.
        names = {field.name for field in self.model._meta.concrete_fields}
        for name in ("creator", "id", "created_at", "updated_at"):
            if name in names and name not in readonly:
                readonly.append(name)
        return readonly

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        # A column that may be NULL or has a default is optional, as in the REST forms (`summary`, `publisher`,
        # `scheme`, an entry's categories): the model's `blank=False` there is historical, not a rule.
        if formfield is not None and (db_field.null or db_field.has_default() or db_field.many_to_many):
            formfield.required = False
        return formfield

    def get_list_display(self, request):
        list_display = super().get_list_display(request)
        if tuple(list_display) == ("__str__",):
            return auto_list_display(self.model)
        return list_display

    def get_list_filter(self, request):
        list_filter = super().get_list_filter(request)
        if not list_filter:
            return auto_list_filter(self.model)
        return list_filter

    def get_search_fields(self, request):
        search_fields = super().get_search_fields(request)
        if not search_fields and not self.big:
            return auto_search_fields(self.model)
        return search_fields

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        # Every FK and one-to-one is a raw-id field unless the admin named it for autocomplete or radios.
        named = set(self.raw_id_fields) | set(self.autocomplete_fields) | set(self.radio_fields)
        auto = tuple(
            field.name
            for field in model._meta.concrete_fields
            if field.is_relation and (field.many_to_one or field.one_to_one) and field.name not in named
        )
        self.raw_id_fields = tuple(self.raw_id_fields) + auto
        # A model's `Meta.ordering` (`seq`, `page`) on a big table sorts the whole table for each page;
        # the primary key is indexed.
        if self.big and not self.ordering:
            self.ordering = ("-pk",)

    @admin.display(description=_("Vectors"))
    def vector_summary(self, obj):
        rows = []
        for field in self.model._meta.concrete_fields:
            if not is_vector(field):
                continue
            value = getattr(obj, field.name, None)
            if value is None:
                rows.append((field.name, "—", ""))
                continue
            values = value.to_list() if hasattr(value, "to_list") else list(value)
            norm = math.sqrt(sum(float(x) * float(x) for x in values))
            rows.append((field.name, f"{len(values)} dims", f"‖v‖ = {norm:.4f}"))
        return format_html(
            "<table class='console-kv'>{}</table>",
            format_html_join("", "<tr><th>{}</th><td>{}</td><td>{}</td></tr>", rows),
        )


class ConsoleModelAdmin(ConsoleMixin, admin.ModelAdmin):
    list_per_page = 50
    show_facets = admin.ShowFacets.ALLOW

    def __init__(self, model, admin_site):
        super().__init__(model, admin_site)
        if self.big:
            self.show_full_result_count = False
            self.paginator = EstimatedCountPaginator
            self.show_facets = admin.ShowFacets.NEVER


def auto_list_display(model) -> Tuple[str, ...]:
    picked: List[str] = ["__str__"]
    for field in model._meta.concrete_fields:
        if field.primary_key or field.name in ("created_at", "updated_at", "password"):
            continue
        if isinstance(field, LIST_SKIP) or is_vector(field):
            continue
        if field.is_relation:
            continue
        picked.append(field.name)
        if len(picked) >= 6:
            break
    if any(field.name == "created_at" for field in model._meta.concrete_fields):
        picked.append("created_at")
    return tuple(picked)


def auto_list_filter(model) -> Tuple[str, ...]:
    picked = []
    for field in model._meta.concrete_fields:
        if field.choices or isinstance(field, models.BooleanField):
            picked.append(field.name)
    return tuple(picked[:6])


def auto_search_fields(model) -> Tuple[str, ...]:
    return tuple(
        field.name
        for field in model._meta.concrete_fields
        if isinstance(field, (models.CharField, models.SlugField)) and field.name in SEARCHABLE_NAMES
    )


def register(*models_, module: str, site=None):
    """`@register(Model, module="library")` — register a console admin under a functional module.

    The class may be a `ConsoleModelAdmin` or any `ModelAdmin` that mixes in `ConsoleMixin` (the user
    admin does). Registration is unconditional; whether the module shows is decided per request.
    """
    from apps.console.modules import BY_KEY

    if module not in BY_KEY:
        raise ValueError(f"Unknown console module {module!r}. Known: {', '.join(BY_KEY)}.")

    def wrapper(admin_class):
        if not issubclass(admin_class, ConsoleMixin):
            raise TypeError(f"{admin_class.__name__} must derive from ConsoleModelAdmin or mix in ConsoleMixin.")
        target = site or admin.site
        admin_class.console_module = module
        for model in models_:
            target.register(model, admin_class)
            target.assign_module(model, module)
        return admin_class

    return wrapper
