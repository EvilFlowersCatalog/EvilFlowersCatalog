"""IP-035: the interactive data grid — any registered model as a server-paged, sortable, filterable table.

The page (`console/grid/<app>/<model>/`) is static HTML plus Tabulator in remote mode; every page of rows
comes from the JSON endpoint (`console/api/grid/<app>/<model>/`), which works on the model admin's own
queryset (vectors already deferred, `apps.console.options`) and its own search fields. So the grid shows
exactly what the classic changelist may show, and nothing a disabled module or a missing view right hides.

What the endpoint allows is whitelisted per column, decided from the field type:

* sorting: any listed column, but on a `big` admin only the primary key and indexed columns (an
  unindexed `ORDER BY` over millions of rows is a full sort);
* filters: choices and booleans exact; numbers and dates with an operator prefix (`>=5`, `<2026-01-01`,
  `2026-01-01..2026-02-01` for a range); text `icontains`, but exact on a `big` admin; foreign keys and
  UUIDs exact; JSON never;
* counts: exact, except an unfiltered `big` table, which answers the planner's estimate (`estimated`).

`?format=csv` streams the same filtered, sorted rows (at most `CSV_LIMIT`).
"""

import csv
import json
import math
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional

from django.contrib.admin.utils import quote
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.db import models
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.urls import NoReverseMatch, path, reverse
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import capfirst

from apps.console.options import estimated_count, heavy_fields, is_vector
from apps.console.views import ApiError, ConsoleApi, ConsolePage, bounded_int

#: Cell text of long fields (text, JSON) is cut here; the change page shows the whole value.
TRUNCATE = 200
CSV_LIMIT = 50_000
PAGE_SIZE_MIN, PAGE_SIZE_MAX, PAGE_SIZE_DEFAULT = 10, 500, 50

NUMBER_FIELDS = (
    models.IntegerField,
    models.BigIntegerField,
    models.SmallIntegerField,
    models.PositiveIntegerField,
    models.PositiveSmallIntegerField,
    models.PositiveBigIntegerField,
    models.FloatField,
    models.DecimalField,
    models.AutoField,
    models.BigAutoField,
)


# --- columns ---------------------------------------------------------------------------------------------


def column_type(field) -> str:
    if field.is_relation:
        return "fk"
    if field.choices:
        return "choice"
    if isinstance(field, models.BooleanField):
        return "bool"
    if isinstance(field, NUMBER_FIELDS):
        return "number"
    if isinstance(field, models.DateTimeField):
        return "datetime"
    if isinstance(field, models.DateField):
        return "date"
    if isinstance(field, models.UUIDField):
        return "uuid"
    if isinstance(field, models.JSONField):
        return "json"
    if isinstance(field, models.TextField):
        return "text"
    if isinstance(field, (models.CharField, models.SlugField, models.EmailField, models.URLField)):
        return "string"
    return "other"


#: Columns no grid ever lists, whatever the admin says: credentials and their hashes.
SECRET_FIELDS = ("password", "passphrase_hash", "lcp_passphrase_hash")


def grid_field_objects(model_admin) -> List[models.Field]:
    """The model's concrete fields the grid lists: `grid_fields` if the admin names them, else all but
    vectors, full-text vectors and binary columns."""
    model = model_admin.model
    candidates = {
        field.name: field
        for field in model._meta.concrete_fields
        if not is_vector(field) and not isinstance(field, (SearchVectorField, models.BinaryField))
    }
    # What the admin's forms hide (`exclude`: bind credentials, passphrase hashes) the grid never shows
    # either — not in the JSON, not in the CSV export — and nor does it show any secret-named column.
    hidden = set(model_admin.get_exclude(None, None) or ()) | set(SECRET_FIELDS)
    candidates = {name: field for name, field in candidates.items() if name not in hidden}
    named = getattr(model_admin, "grid_fields", None)
    if named:
        return [candidates[name] for name in named if name in candidates]
    fields = list(candidates.values())
    # The admin's own list columns first, in its order; the rest follow (hidden, in the column chooser).
    listed = list_display_names(model_admin)
    return sorted(fields, key=lambda field: listed.index(field.name) if field.name in listed else len(listed))


def list_display_names(model_admin) -> List[str]:
    """Concrete fields named in the admin's `list_display` (methods and `__str__` are not columns)."""
    concrete = {field.name for field in model_admin.model._meta.concrete_fields}
    return [name for name in model_admin.list_display if isinstance(name, str) and name in concrete]


def sortable(model_admin, field) -> bool:
    if column_type(field) in ("json", "other"):
        return False
    if getattr(model_admin, "big", False):
        return bool(field.primary_key or field.db_index or field.unique or field.is_relation)
    return True


def filterable(model_admin, field) -> bool:
    return column_type(field) not in ("json", "other")


def default_visible(field, index: int) -> bool:
    """Show the first handful of short columns; long text, JSON and bookkeeping start hidden."""
    if column_type(field) in ("json", "text"):
        return False
    if field.name in ("updated_at",):
        return False
    return index < 9


def columns(model_admin) -> List[dict]:
    result = []
    listed = [] if getattr(model_admin, "grid_fields", None) else list_display_names(model_admin)
    for index, field in enumerate(grid_field_objects(model_admin)):
        visible = field.name in listed if listed else default_visible(field, index)
        spec = {
            "field": field.name,
            "title": str(capfirst(field.verbose_name)),
            "type": column_type(field),
            "sortable": sortable(model_admin, field),
            "filterable": filterable(model_admin, field),
            "visible": visible,
            "primary_key": field.primary_key,
        }
        if field.choices:
            spec["choices"] = [[str(value), str(label)] for value, label in field.flatchoices]
        result.append(spec)
    return result


# --- values ----------------------------------------------------------------------------------------------


def truncate(text: str) -> str:
    return text if len(text) <= TRUNCATE else text[: TRUNCATE - 1] + "…"


def cell(field, value):
    """A JSON-safe, short cell value."""
    if value is None:
        return None
    kind = column_type(field)
    if kind == "json":
        return truncate(json.dumps(value, ensure_ascii=False, default=str))
    if kind == "text":
        return truncate(str(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (bool, int, float, str)):
        return truncate(value) if isinstance(value, str) else value
    return truncate(str(value))


def label_of(obj) -> str:
    try:
        return str(obj)
    except Exception:  # pragma: no cover - a broken __str__ must not break a page of rows
        return str(obj.pk)


def change_url(site, model, pk) -> Optional[str]:
    try:
        return reverse(
            f"{site.name}:{model._meta.app_label}_{model._meta.model_name}_change",
            args=[quote(pk)],
            current_app=site.name,
        )
    except NoReverseMatch:
        return None


def related_labels(site, field, ids: Iterable) -> Dict[str, dict]:
    """One query per foreign key per page: labels (and admin links) of the referenced rows."""
    ids = {value for value in ids if value is not None}
    if not ids:
        return {}
    related = field.related_model
    target = field.target_field.attname
    queryset = related._default_manager.filter(**{f"{target}__in": ids})
    heavy = heavy_fields(related)
    if heavy:
        queryset = queryset.defer(*heavy)
    # Defer long text too: a label needs a title, not a book.
    long_text = [f.name for f in related._meta.concrete_fields if isinstance(f, models.TextField)]
    if long_text:
        queryset = queryset.defer(*long_text)
    linkable = related in site._registry
    out = {}
    for obj in queryset:
        key = str(getattr(obj, target))
        out[key] = {"label": label_of(obj), "url": change_url(site, related, obj.pk) if linkable else None}
    return out


def serialise(site, model_admin, rows: List[models.Model], fields: List[models.Field]) -> List[dict]:
    fks = [field for field in fields if field.is_relation]
    labels = {field.name: related_labels(site, field, (getattr(row, field.attname) for row in rows)) for field in fks}
    data = []
    for row in rows:
        item = {"_pk": str(row.pk), "_url": change_url(site, model_admin.model, row.pk), "_label": label_of(row)}
        for field in fields:
            if field.is_relation:
                raw = getattr(row, field.attname)
                if raw is None:
                    item[field.name] = None
                else:
                    known = labels[field.name].get(str(raw), {})
                    item[field.name] = {"id": str(raw), "label": known.get("label", str(raw)), "url": known.get("url")}
            else:
                item[field.name] = cell(field, getattr(row, field.attname))
        data.append(item)
    return data


# --- filtering -------------------------------------------------------------------------------------------

OPERATORS = ((">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt"), ("=", "exact"))


def split_operator(raw: str):
    raw = raw.strip()
    for symbol, lookup in OPERATORS:
        if raw.startswith(symbol):
            return lookup, raw[len(symbol) :].strip()
    return "exact", raw


def parse_number(field, raw: str):
    try:
        if isinstance(field, models.DecimalField):
            return Decimal(raw)
        if isinstance(field, models.FloatField):
            return float(raw)
        return int(raw)
    except (ValueError, InvalidOperation):
        raise ApiError(f"'{raw}' is not a number for {field.name}.")


def parse_moment(field, raw: str):
    value = parse_datetime(raw) if isinstance(field, models.DateTimeField) else None
    if value is None:
        value = parse_date(raw)
    if value is None:
        raise ApiError(f"'{raw}' is not a date (YYYY-MM-DD) for {field.name}.")
    return value


def field_filter(model_admin, field, raw) -> models.Q:
    """The `Q` for one header filter; `ApiError` for a value the column cannot take."""
    kind = column_type(field)
    raw = "" if raw is None else str(raw).strip()
    if raw == "":
        return models.Q()
    if raw in ("∅", "null", "NULL") and field.null:
        return models.Q(**{f"{field.name}__isnull": True})
    if kind == "bool":
        if raw.lower() in ("true", "1", "yes"):
            return models.Q(**{field.name: True})
        if raw.lower() in ("false", "0", "no"):
            return models.Q(**{field.name: False})
        raise ApiError(f"'{raw}' is not a boolean for {field.name}.")
    if kind == "choice":
        return models.Q(**{field.name: raw})
    if kind in ("fk", "uuid"):
        target = field.target_field if field.is_relation else field
        try:
            value = target.to_python(raw)
        except ValidationError:
            raise ApiError(f"'{raw}' is not a valid id for {field.name}.")
        return models.Q(**{field.attname if field.is_relation else field.name: value})
    if kind == "number":
        lookup, value = split_operator(raw)
        return models.Q(**{f"{field.name}__{lookup}": parse_number(field, value)})
    if kind in ("date", "datetime"):
        if ".." in raw:
            start, end = (part.strip() for part in raw.split("..", 1))
            q = models.Q()
            if start:
                q &= models.Q(**{f"{field.name}__gte": parse_moment(field, start)})
            if end:
                end_value = parse_moment(field, end)
                # A bare end date includes that whole day.
                lookup = "lte" if isinstance(end_value, datetime) else ("date__lte" if kind == "datetime" else "lte")
                q &= models.Q(**{f"{field.name}__{lookup}": end_value})
            return q
        lookup, value = split_operator(raw)
        moment = parse_moment(field, value)
        if kind == "datetime" and not isinstance(moment, datetime):
            lookup = f"date__{lookup}"
        return models.Q(**{f"{field.name}__{lookup}": moment})
    if kind in ("string", "text"):
        if getattr(model_admin, "big", False):
            return models.Q(**{field.name: raw})
        return models.Q(**{f"{field.name}__icontains": raw})
    raise ApiError(f"{field.name} cannot be filtered.")


def indexed_params(params, prefix: str) -> List[dict]:
    """Tabulator's `sort[0][field]=…&sort[0][dir]=…` → `[{"field": …, "dir": …}]`, in index order."""
    found: Dict[int, dict] = {}
    for key in params.keys():
        if not key.startswith(prefix + "["):
            continue
        try:
            index_part, name_part = key[len(prefix) + 1 :].split("][", 1)
            index = int(index_part)
        except ValueError:
            continue
        found.setdefault(index, {})[name_part.rstrip("]")] = params.get(key)
    return [found[index] for index in sorted(found)]


# --- views -----------------------------------------------------------------------------------------------


class GridMixin:
    site = None

    def resolve(self, request, app_label: str, model_name: str):
        for model, model_admin in self.site._registry.items():
            if model._meta.app_label == app_label and model._meta.model_name == model_name:
                if not model_admin.has_view_permission(request):
                    raise Http404("This table is not available.")
                self.module = self.site.module_of(model)
                return model, model_admin
        raise Http404("No such table in the console.")


class GridPage(GridMixin, ConsolePage):
    template_name = "console/grid.html"

    def dispatch(self, request, *args, **kwargs):
        self.model, self.model_admin = self.resolve(request, kwargs["app_label"], kwargs["model_name"])
        return super().dispatch(request, *args, **kwargs)

    def get_title(self):
        return capfirst(self.model._meta.verbose_name_plural)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        opts = self.model._meta
        context.update(
            {
                "opts": opts,
                "grid_columns": columns(self.model_admin),
                "grid_api_url": reverse(
                    f"{self.site.name}:console_grid_api",
                    args=[opts.app_label, opts.model_name],
                    current_app=self.site.name,
                ),
                "grid_storage_key": f"console-grid:{opts.label_lower}",
                "grid_big": bool(getattr(self.model_admin, "big", False)),
                "grid_searchable": bool(self.model_admin.get_search_fields(self.request)),
                "changelist_url": reverse(
                    f"{self.site.name}:{opts.app_label}_{opts.model_name}_changelist", current_app=self.site.name
                ),
                "add_url": (
                    reverse(f"{self.site.name}:{opts.app_label}_{opts.model_name}_add", current_app=self.site.name)
                    if self.model_admin.has_add_permission(self.request)
                    else None
                ),
            }
        )
        return context


class GridApi(GridMixin, ConsoleApi):
    def get(self, request, app_label, model_name):
        model, model_admin = self.resolve(request, app_label, model_name)
        fields = grid_field_objects(model_admin)
        by_name = {field.name: field for field in fields}
        queryset, filtered = self.queryset(request, model_admin, by_name)
        if request.GET.get("format") == "csv":
            return self.csv(model, queryset, fields)

        size = bounded_int(request.GET.get("size"), PAGE_SIZE_DEFAULT, PAGE_SIZE_MIN, PAGE_SIZE_MAX)
        page = bounded_int(request.GET.get("page"), 1, 1, 10**9)
        estimated = False
        if getattr(model_admin, "big", False) and not filtered:
            total = estimated_count(model)
            estimated = total >= 0
            if total < 0:
                total = queryset.count()
        else:
            total = queryset.count()
        last_page = max(1, math.ceil(total / size)) if total else 1
        offset = (page - 1) * size
        rows = list(queryset[offset : offset + size])
        # An estimate can undershoot: never claim the last page while a full page came back.
        if estimated and len(rows) == size and page >= last_page:
            last_page = page + 1
        return JsonResponse(
            {
                "last_page": last_page,
                "last_row": total,
                "total": total,
                "estimated": estimated,
                "page": page,
                "size": size,
                "data": serialise(self.site, model_admin, rows, fields),
            },
            json_dumps_params={"default": str},
        )

    def queryset(self, request, model_admin, by_name):
        """The admin's queryset with the request's search, filters and sort; and whether anything filters it."""
        queryset = model_admin.get_queryset(request)
        filtered = False

        q = (request.GET.get("q") or "").strip()
        if q:
            queryset, duplicates = model_admin.get_search_results(request, queryset, q)
            if duplicates:
                queryset = queryset.distinct()
            filtered = True

        condition = models.Q()
        for item in indexed_params(request.GET, "filter"):
            field = by_name.get(item.get("field") or "")
            if field is None or not filterable(model_admin, field):
                raise ApiError(f"Column '{item.get('field')}' cannot be filtered.")
            part = field_filter(model_admin, field, item.get("value"))
            if part:
                condition &= part
                filtered = True
        if condition:
            queryset = queryset.filter(condition)

        ordering = []
        for item in indexed_params(request.GET, "sort"):
            field = by_name.get(item.get("field") or "")
            if field is None or not sortable(model_admin, field):
                raise ApiError(f"Column '{item.get('field')}' cannot be sorted.")
            prefix = "-" if item.get("dir") == "desc" else ""
            ordering.append(f"{prefix}{field.attname if field.is_relation else field.name}")
        if not ordering:
            ordering = [
                part
                for part in (model_admin.get_ordering(request) or ())
                if isinstance(part, str) and part.lstrip("-").split("__")[0] in by_name
            ]
        if not any(part.lstrip("-") in ("pk", model_admin.model._meta.pk.name) for part in ordering):
            ordering.append("-pk")
        return queryset.order_by(*ordering), filtered

    def csv(self, model, queryset, fields):
        class Echo:
            def write(self, value):
                return value

        writer = csv.writer(Echo())

        def value(field, row):
            raw = getattr(row, field.attname)
            if raw is None:
                return ""
            if isinstance(raw, (dict, list)):
                return json.dumps(raw, ensure_ascii=False, default=str)
            if isinstance(raw, (datetime, date)):
                return raw.isoformat()
            return str(raw)

        def rows():
            yield "﻿"  # Excel reads the BOM as UTF-8.
            yield writer.writerow([field.attname for field in fields])
            for row in queryset[:CSV_LIMIT].iterator(chunk_size=2000):
                yield writer.writerow([value(field, row) for field in fields])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{model._meta.label_lower}.csv"'
        return response


def urlpatterns(site):
    return [
        path(
            "console/grid/<slug:app_label>/<slug:model_name>/",
            site.admin_view(GridPage.as_view(site=site)),
            name="console_grid",
        ),
        path(
            "console/api/grid/<slug:app_label>/<slug:model_name>/",
            site.admin_view(GridApi.as_view(site=site)),
            name="console_grid_api",
        ),
    ]


__all__ = ["GridPage", "GridApi", "columns", "urlpatterns"]
