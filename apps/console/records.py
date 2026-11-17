"""IP-035: the record page — what the console shows above and beside a change form.

Every console admin gets a record page: a hero (picture or initials, the module and type, title, subtitle,
badges, actions), a row of key figures, the form's fieldsets and inlines as tabs, and a side panel with the
rows that point at the record (counted, each a link to its filtered list), short lists of recent activity
and the record's own facts. `ConsoleMixin.record()` builds the default; an admin with something to say
about its model (Entry, Catalog, User) extends it.

The related panel is derived from the model's reverse relations, so it stays complete when a model is added:
every registered, visible model with a foreign key or many-to-many to this one, with its count.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from django.db.models import ForeignObjectRel
from django.urls import NoReverseMatch, reverse
from django.utils.formats import date_format
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

#: How many related lists the side panel shows at most (the largest counts first).
RELATED_LIMIT = 16


@dataclass
class Badge:
    label: str
    tone: str = "muted"  # ok, warn, bad, info, muted


@dataclass
class Stat:
    label: str
    value: object
    hint: str = ""
    url: Optional[str] = None
    tone: str = ""  # "" or "accent"


@dataclass
class Action:
    label: str
    url: str
    icon: str = "grid"
    external: bool = False
    primary: bool = False


@dataclass
class Related:
    label: str
    count: int
    url: Optional[str]
    module: str = ""


@dataclass
class ListItem:
    label: str
    url: Optional[str] = None
    meta: str = ""
    badge: Optional[Badge] = None


@dataclass
class RecordList:
    title: str
    items: List[ListItem]
    more_url: Optional[str] = None
    empty: str = ""


@dataclass
class Record:
    kind: str
    title: str
    eyebrow: str = ""
    subtitle: str = ""
    image: Optional[str] = None
    initials: str = ""
    icon: str = "grid"
    badges: List[Badge] = field(default_factory=list)
    stats: List[Stat] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    related: List[Related] = field(default_factory=list)
    lists: List[RecordList] = field(default_factory=list)
    facts: List[tuple] = field(default_factory=list)


def tone(text: str) -> int:
    """A stable colour (1–8, the `--e-series-*` palette) for an initials tile, so names tell apart at a glance."""
    import zlib

    return zlib.crc32(str(text or "").encode()) % 8 + 1


def initials(text: str) -> str:
    words = [word for word in str(text or "").replace("_", " ").split() if word[:1].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def changelist_url(model, **filters) -> Optional[str]:
    try:
        url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    except NoReverseMatch:
        return None
    return f"{url}?{urlencode(filters)}" if filters else url


def change_url(obj) -> Optional[str]:
    if obj is None:
        return None
    try:
        return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])
    except NoReverseMatch:
        return None


def when(value) -> str:
    return date_format(value, "DATETIME_FORMAT") if value else "—"


def related_records(site, request, obj) -> List[Related]:
    """Every visible registered model pointing at `obj`, with its count and a link to the filtered list."""
    found = []
    for relation in obj._meta.get_fields():
        if not isinstance(relation, ForeignObjectRel):
            continue
        related_model = relation.related_model
        model_admin = site._registry.get(related_model)
        if model_admin is None or not model_admin.has_view_permission(request):
            continue
        field_name = relation.field.name
        target = relation.field.target_field.name if not relation.many_to_many else obj._meta.pk.name
        lookup = f"{field_name}__{target}__exact"
        try:
            count = related_model._default_manager.filter(**{field_name: obj}).count()
        except Exception:  # pragma: no cover - an exotic relation must not break the page
            continue
        if not count:
            continue
        label = related_model._meta.verbose_name_plural
        if sum(1 for other in found if other.label == label):
            label = f"{label} ({relation.field.verbose_name})"
        found.append(
            Related(
                label=str(label).capitalize(),
                count=count,
                url=changelist_url(related_model, **{lookup: obj.pk}),
                module=site.module_of(related_model) or "",
            )
        )
    found.sort(key=lambda item: (-item.count, item.label))
    return found[:RELATED_LIMIT]


def default_record(model_admin, request, obj) -> Record:
    from apps.console.modules import BY_KEY

    opts = obj._meta
    module = BY_KEY.get(model_admin.console_module or "")
    record = Record(
        kind=str(opts.verbose_name),
        title=str(obj),
        eyebrow=f"{module.title} · {opts.verbose_name}" if module else str(opts.verbose_name),
        initials=initials(str(obj)),
        icon=module.icon if module else "grid",
    )
    facts = [(_("ID"), str(obj.pk))]
    for name, label in (("created_at", _("Created")), ("updated_at", _("Updated"))):
        if hasattr(obj, name):
            facts.append((label, when(getattr(obj, name))))
    creator = getattr(obj, "creator", None) if hasattr(obj, "creator_id") else None
    if creator is not None:
        facts.append((_("Created by"), str(creator)))
    record.facts = facts
    record.related = related_records(model_admin.admin_site, request, obj)
    try:
        history = reverse(f"admin:{opts.app_label}_{opts.model_name}_history", args=[obj.pk])
        record.actions.append(Action(_("History"), history, icon="list"))
    except NoReverseMatch:
        pass
    return record


def new_record(model_admin) -> Record:
    from apps.console.modules import BY_KEY

    opts = model_admin.model._meta
    module = BY_KEY.get(model_admin.console_module or "")
    return Record(
        kind=str(opts.verbose_name),
        title=_("New %(name)s") % {"name": opts.verbose_name},
        eyebrow=f"{module.title} · {opts.verbose_name}" if module else str(opts.verbose_name),
        initials="+",
        icon=module.icon if module else "grid",
    )


@dataclass
class Card:
    """One tile of a changelist's gallery view."""

    title: str
    url: Optional[str]
    subtitle: str = ""
    image: Optional[str] = None
    initials: str = ""
    badges: List[Badge] = field(default_factory=list)
    meta: str = ""
