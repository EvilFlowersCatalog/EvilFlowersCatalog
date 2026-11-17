"""IP-035: record pages and gallery cards of the core models the console is mostly used for — publications,
catalogs and people. Each extends the default record (`apps.console.records.default_record`) with what a
librarian looks for first, in a few cheap queries."""

from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from apps.console.records import (
    Action,
    Badge,
    Card,
    ListItem,
    RecordList,
    Stat,
    change_url,
    changelist_url,
    initials,
)

ACTIVE_LICENSES = ("ready", "active")
WAITING_RESERVATIONS = ("queued", "available")
LICENSE_TONES = {"active": "ok", "ready": "info", "returned": "muted", "expired": "muted", "revoked": "bad"}
JOB_TONES = {
    "completed": "ok",
    "succeeded": "ok",
    "failed": "bad",
    "running": "info",
    "queued": "warn",
    "pending": "warn",
}


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def thumbnail_url(entry):
    if not entry.thumbnail:
        return None
    url = _url("files:thumbnail-download", entry.pk)
    return f"{url}?v={entry.thumbnail_hash or ''}" if url else None


def author_names(entry, limit=4):
    names = [f"{author.name} {author.surname}".strip() for author in entry.authors.all()[: limit + 1]]
    if len(names) > limit:
        names = names[:limit] + ["…"]
    return ", ".join(names)


def _year(entry):
    published = entry.published_at
    if not published:
        return ""
    return str(getattr(published, "year", None) or str(published)[:4])


# --- Entry -------------------------------------------------------------------------------------------------


def entry_record(record, request, entry):
    from apps.readium.models import License, Reservation

    config = entry.config or {}
    record.image = thumbnail_url(entry)
    record.eyebrow = " · ".join(
        part
        for part in (str(_("Publication")), entry.catalog.title, entry.language.name if entry.language else "")
        if part
    )
    record.subtitle = " · ".join(part for part in (author_names(entry), entry.publisher or "", _year(entry)) if part)

    if entry.withdrawn_at:
        record.badges.append(Badge(_("withdrawn"), "bad"))
    record.badges.append(Badge(_("public catalog"), "ok") if entry.catalog.is_public else Badge(_("private catalog")))
    if config.get("readium_enabled"):
        record.badges.append(Badge(_("LCP · %(n)s copies") % {"n": config.get("readium_amount") or 1}, "info"))
    if config.get("evilflowers_ocr_enabled"):
        record.badges.append(Badge(_("OCR"), "info"))
    if entry.origin and entry.origin != "elvira":
        record.badges.append(Badge(_("harvested: %(origin)s") % {"origin": entry.origin}, "warn"))
    for key in ("isbn", "doi"):
        if (entry.identifiers or {}).get(key):
            record.badges.append(Badge(f"{key.upper()} {entry.identifiers[key]}"))

    licenses = License.objects.filter(entry=entry)
    active = licenses.filter(state__in=ACTIVE_LICENSES).count()
    waiting = Reservation.objects.filter(entry=entry, status__in=WAITING_RESERVATIONS).count()
    files = entry.acquisitions.count()
    rating = f"{entry.rating_sum / entry.rating_count:.1f}" if entry.rating_count else "—"
    record.stats = [
        Stat(_("Popularity"), entry.popularity, tone="accent"),
        Stat(
            _("Rating"),
            rating,
            _("%(n)s reviews") % {"n": entry.rating_count},
            changelist_url(entry.reviews.model, entry__id__exact=entry.pk),
        ),
        Stat(
            _("On loan"),
            active,
            _("of %(n)s copies") % {"n": config.get("readium_amount") or "—"} if config.get("readium_enabled") else "",
            changelist_url(License, entry__id__exact=entry.pk),
        ),
        Stat(_("Waiting"), waiting, _("reservations"), changelist_url(Reservation, entry__id__exact=entry.pk)),
        Stat(_("Files"), files, url=changelist_url(entry.acquisitions.model, entry__id__exact=entry.pk)),
        Stat(_("Pages"), entry.page_count or "—"),
    ]

    from apps.dataverse.services.links import portal_entry_url

    portal = portal_entry_url(entry.pk)
    actions = []
    if portal:
        actions.append(Action(_("Open in portal"), portal, icon="book", external=True, primary=True))
    graph = _url("admin:console_graph")
    if graph:
        actions.append(Action(_("Graph"), f"{graph}?node=entry:{entry.pk}", icon="share"))
    if entry.embedding_model:
        vectors = _url("admin:console_vectors")
        if vectors:
            actions.append(Action(_("Similar"), f"{vectors}?entry={entry.pk}", icon="spark"))
    record.actions = actions + record.actions

    loans = licenses.select_related("user").order_by("-created_at")[:5]
    record.lists.append(
        RecordList(
            _("Loans"),
            [
                ListItem(
                    str(license.user),
                    change_url(license),
                    meta=(
                        _("until %(date)s") % {"date": date_format(license.expires_at, "SHORT_DATE_FORMAT")}
                        if license.expires_at
                        else ""
                    ),
                    badge=Badge(license.get_state_display(), LICENSE_TONES.get(license.state, "muted")),
                )
                for license in loans
            ],
            more_url=changelist_url(License, entry__id__exact=entry.pk),
            empty=_("Never lent."),
        )
    )
    try:
        from apps.tasks.models import Job

        jobs = Job.objects.filter(entry=entry).order_by("-last_transition_at")[:5]
        record.lists.append(
            RecordList(
                _("Jobs"),
                [
                    ListItem(
                        job.kind,
                        change_url(job),
                        meta=(
                            date_format(job.last_transition_at, "SHORT_DATETIME_FORMAT")
                            if job.last_transition_at
                            else ""
                        ),
                        badge=Badge(job.state, JOB_TONES.get(job.state, "muted")),
                    )
                    for job in jobs
                ],
                more_url=changelist_url(Job, entry__id__exact=entry.pk),
                empty=_("No background work recorded."),
            )
        )
    except Exception:  # pragma: no cover - the jobs app is always installed; keep the page alive regardless
        pass
    record.facts.insert(1, (_("Catalog"), entry.catalog.title))
    return record


def entry_card(entry):
    badges = []
    if entry.withdrawn_at:
        badges.append(Badge(_("withdrawn"), "bad"))
    if (entry.config or {}).get("readium_enabled"):
        badges.append(Badge(_("LCP"), "info"))
    return Card(
        title=entry.title,
        url=change_url(entry),
        subtitle=" · ".join(part for part in (entry.catalog.title, _year(entry)) if part),
        image=thumbnail_url(entry),
        initials=initials(entry.title),
        badges=badges,
        meta=_("popularity %(n)s") % {"n": entry.popularity},
    )


# --- Catalog -----------------------------------------------------------------------------------------------


def catalog_record(record, request, catalog):
    from apps.core.models import Category, Entry, Feed, UserCatalog

    record.eyebrow = f"{_('Catalog')} · /{catalog.url_name}"
    record.badges.append(Badge(_("public"), "ok") if catalog.is_public else Badge(_("private")))
    if catalog.harvestable:
        record.badges.append(Badge(_("harvestable (OAI-PMH)"), "info"))
    record.badges.append(
        Badge(
            _("policy: %(key)s") % {"key": catalog.content_policy} if catalog.content_policy else _("default policy")
        )
    )

    entries = Entry.objects.filter(catalog=catalog)
    figures = entries.aggregate(
        total=Count("id"),
        withdrawn=Count("id", filter=Q(withdrawn_at__isnull=False)),
        covers=Count("id", filter=~Q(thumbnail="")),
    )
    members = UserCatalog.objects.filter(catalog=catalog)
    record.stats = [
        Stat(
            _("Publications"),
            figures["total"],
            _("%(n)s withdrawn") % {"n": figures["withdrawn"]} if figures["withdrawn"] else "",
            changelist_url(Entry, catalog__id__exact=catalog.pk),
            tone="accent",
        ),
        Stat(_("With cover"), figures["covers"]),
        Stat(
            _("Categories"),
            Category.objects.filter(catalog=catalog).count(),
            url=changelist_url(Category, catalog__id__exact=catalog.pk),
        ),
        Stat(
            _("Feeds"),
            Feed.objects.filter(catalog=catalog).count(),
            url=changelist_url(Feed, catalog__id__exact=catalog.pk),
        ),
        Stat(
            _("Members"),
            members.count(),
            _("%(n)s manage") % {"n": members.filter(mode=UserCatalog.Mode.MANAGE).count()},
            changelist_url(UserCatalog, catalog__id__exact=catalog.pk),
        ),
    ]
    opds = _url("opds:root", catalog.url_name)
    actions = [
        Action(_("Publications"), changelist_url(Entry, catalog__id__exact=catalog.pk), icon="book", primary=True)
    ]
    if opds:
        actions.append(Action(_("OPDS feed"), opds, icon="share", external=True))
    record.actions = actions + record.actions

    recent = entries.order_by("-created_at")[:6]
    record.lists.append(
        RecordList(
            _("Latest publications"),
            [
                ListItem(entry.title, change_url(entry), meta=date_format(entry.created_at, "SHORT_DATE_FORMAT"))
                for entry in recent
            ],
            more_url=changelist_url(Entry, catalog__id__exact=catalog.pk),
            empty=_("No publications yet."),
        )
    )
    top = Category.objects.filter(catalog=catalog).annotate(n=Count("entries")).filter(n__gt=0).order_by("-n")[:6]
    record.lists.append(
        RecordList(
            _("Largest categories"),
            [
                ListItem(
                    category.term,
                    changelist_url(Entry, categories__id__exact=category.pk),
                    meta=_("%(n)s publications") % {"n": category.n},
                )
                for category in top
            ],
            empty=_("Nothing classified yet."),
        )
    )
    return record


def catalog_card(catalog):
    return Card(
        title=catalog.title,
        url=change_url(catalog),
        subtitle=f"/{catalog.url_name}",
        initials=initials(catalog.title),
        badges=[Badge(_("public"), "ok") if catalog.is_public else Badge(_("private"))],
        meta=_("%(n)s publications") % {"n": getattr(catalog, "entry_count", "—")},
    )


# --- User --------------------------------------------------------------------------------------------------


def user_record(record, request, user):
    from apps.core.models import ApiKey, UserCatalog
    from apps.readium.models import License, Reservation

    full_name = f"{user.name} {user.surname}".strip()
    record.title = full_name or user.username
    record.subtitle = user.username if full_name else ""
    record.initials = initials(full_name or user.username)
    record.eyebrow = f"{_('Person')} · {user.auth_source}"
    if user.is_superuser:
        record.badges.append(Badge(_("superuser"), "info"))
    record.badges.append(Badge(_("active"), "ok") if user.is_active else Badge(_("deactivated"), "bad"))
    if user.password_change_required:
        record.badges.append(Badge(_("must change password"), "warn"))
    if not user.password or not user.has_usable_password():
        record.badges.append(Badge(_("no local password")))

    licenses = License.objects.filter(user=user)
    access = UserCatalog.objects.filter(user=user).select_related("catalog")
    record.stats = [
        Stat(
            _("On loan"),
            licenses.filter(state__in=ACTIVE_LICENSES).count(),
            url=changelist_url(License, user__id__exact=user.pk),
            tone="accent",
        ),
        Stat(_("Borrowed ever"), licenses.count()),
        Stat(
            _("Waiting"),
            Reservation.objects.filter(user=user, status__in=WAITING_RESERVATIONS).count(),
            url=changelist_url(Reservation, user__id__exact=user.pk),
        ),
        Stat(_("Catalogs"), access.count(), url=changelist_url(UserCatalog, user__id__exact=user.pk)),
        Stat(
            _("API keys"),
            ApiKey.objects.filter(user=user, is_active=True).count(),
            url=changelist_url(ApiKey, user__id__exact=user.pk),
        ),
        Stat(_("Last login"), date_format(user.last_login, "SHORT_DATE_FORMAT") if user.last_login else "—"),
    ]
    # Django's UserAdmin names this route `auth_user_password_change` whatever the user model is.
    password = _url("admin:auth_user_password_change", user.pk)
    if password:
        record.actions.insert(0, Action(_("Set password"), password, icon="key"))

    loans = licenses.filter(state__in=ACTIVE_LICENSES).select_related("entry").order_by("expires_at")[:5]
    record.lists.append(
        RecordList(
            _("Current loans"),
            [
                ListItem(
                    license.entry.title,
                    change_url(license),
                    meta=(
                        _("until %(date)s") % {"date": date_format(license.expires_at, "SHORT_DATE_FORMAT")}
                        if license.expires_at
                        else ""
                    ),
                    badge=Badge(license.get_state_display(), LICENSE_TONES.get(license.state, "muted")),
                )
                for license in loans
            ],
            more_url=changelist_url(License, user__id__exact=user.pk),
            empty=_("Nothing on loan."),
        )
    )
    record.lists.append(
        RecordList(
            _("Catalog access"),
            [
                ListItem(
                    item.catalog.title,
                    change_url(item.catalog),
                    badge=Badge(item.get_mode_display(), "info" if item.mode == UserCatalog.Mode.MANAGE else "muted"),
                )
                for item in access[:8]
            ],
            more_url=changelist_url(UserCatalog, user__id__exact=user.pk),
            empty=_("No catalog memberships (public catalogs only)."),
        )
    )
    return record


def user_card(user):
    full_name = f"{user.name} {user.surname}".strip()
    badges = [Badge(_("superuser"), "info")] if user.is_superuser else []
    if not user.is_active:
        badges.append(Badge(_("deactivated"), "bad"))
    return Card(
        title=full_name or user.username,
        url=change_url(user),
        subtitle=user.username,
        initials=initials(full_name or user.username),
        badges=badges,
        meta=(
            _("last login %(date)s") % {"date": date_format(user.last_login, "SHORT_DATE_FORMAT")}
            if user.last_login
            else _("never logged in")
        ),
    )
