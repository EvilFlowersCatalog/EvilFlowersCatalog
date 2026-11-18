"""IP-035: the core models in the console — library, people & access, reading activity, system.

The library and accounts have full CRUD. Their side effects (search vectors, OAI records, harvest sync,
file cleanup, LCP and extraction queues) are model signals, so an admin save triggers exactly what a REST
save does; what the REST views do *besides* saving is repeated here (the catalog creator's `manage` grant,
the entry duplicate rule, the feed slug rule, covers and files through `apps.api.services.entry`).
Readers' activity stays read-only (it is their record, not the library's).
"""

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import Group
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleMixin, ConsoleModelAdmin, register
from apps.core import settings_store
from apps.core.models import (
    Acquisition,
    Annotation,
    AnnotationItem,
    ApiKey,
    AuthSource,
    Author,
    Catalog,
    Category,
    Currency,
    Entry,
    EntryAuthor,
    EntryReview,
    Feed,
    Language,
    Price,
    ReadEvent,
    Setting,
    ShelfRecord,
    User,
    UserAcquisition,
    UserActivity,
    UserCatalog,
)
from apps.core.services.reviews import ReviewService

# --- Library -------------------------------------------------------------------------------------------


class ConsoleInlineMixin:
    """Inline rows follow their parent admin's write rights (module on, deployment not read-only)."""

    def _writes(self, request):
        parent = self.admin_site._registry.get(self.parent_model)
        return parent is not None and parent.has_change_permission(request)

    def has_add_permission(self, request, obj=None):
        return self._writes(request)

    def has_change_permission(self, request, obj=None):
        return self._writes(request)

    def has_delete_permission(self, request, obj=None):
        return self._writes(request)

    def has_view_permission(self, request, obj=None):
        parent = self.admin_site._registry.get(self.parent_model)
        return parent is not None and parent.has_view_permission(request, obj)


class UserCatalogInline(ConsoleInlineMixin, admin.TabularInline):
    """Who may read, write or manage the catalog (the REST `users` field of a catalog)."""

    model = UserCatalog
    extra = 0
    fields = ("user", "mode")
    raw_id_fields = ("user",)
    verbose_name_plural = _("Access")


class CatalogForm(forms.ModelForm):
    """The content policy is one of the stored policies (`assign_content_policy` checks the same)."""

    content_policy = forms.ChoiceField(required=False, label=_("Content policy"))

    class Meta:
        model = Catalog
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.content import policies

        choices = [("", _("— instance default —"))]
        choices += [(policy.key, f"{policy.title} ({policy.key})") for policy in policies.all_current()]
        self.fields["content_policy"].choices = choices

    def clean_content_policy(self):
        return self.cleaned_data.get("content_policy") or None


@register(Catalog, module="library")
class CatalogAdmin(ConsoleModelAdmin):
    """Full CRUD. Like `POST /api/v1/catalogs`, the creator gets `manage` on a new catalog.

    Deleting a catalog deletes its entries and their files (Django's confirmation lists them).
    """

    editable = True
    deletable = True
    form = CatalogForm
    list_display = ("title", "url_name", "is_public", "harvestable", "content_policy", "entries", "created_at")
    list_filter = ("is_public", "harvestable", "content_policy")
    search_fields = ("title", "url_name")
    prepopulated_fields = {"url_name": ("title",)}
    fields = ("title", "url_name", "is_public", "harvestable", "content_policy", "creator", "touched_at")
    readonly_fields = ("touched_at",)
    inlines = (UserCatalogInline,)

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(entry_count=Count("entries", distinct=True))

    gallery = True

    @admin.display(description=_("Entries"), ordering="entry_count")
    def entries(self, obj):
        return obj.entry_count

    def record(self, request, obj):
        from apps.core.console_records import catalog_record

        return catalog_record(super().record(request, obj), request, obj)

    def card(self, request, obj):
        from apps.core.console_records import catalog_card

        return catalog_card(obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            UserCatalog.objects.get_or_create(
                catalog=obj, user=request.user, defaults={"mode": UserCatalog.Mode.MANAGE}
            )


class EntryAuthorInline(ConsoleInlineMixin, admin.TabularInline):
    """Authors in order. Saving a row rebuilds the entry's search text and OAI record (signals)."""

    model = EntryAuthor
    extra = 0
    fields = ("position", "author")
    raw_id_fields = ("author",)
    ordering = ("position",)


class AcquisitionInline(admin.TabularInline):
    """Files are added on the Acquisition page (upload goes through `attach_acquisition`)."""

    model = Acquisition
    extra = 0
    can_delete = False
    fields = ("relation", "mime", "storage_backend", "content", "created_at")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")


class EntryForm(forms.ModelForm):
    """The REST entry rules a bare form would skip: the duplicate rule (title, ISBN or DOI in the catalog)
    and the cover upload (stored with its thumbnail by `set_cover`)."""

    cover_upload = forms.ImageField(
        label=_("New cover"), required=False, help_text=_("JPEG, PNG, WebP or GIF; the thumbnail is derived.")
    )

    class Meta:
        model = Entry
        fields = "__all__"

    def clean(self):
        from functools import reduce
        from operator import or_

        from django.db.models import Q

        cleaned = super().clean()
        catalog, title = cleaned.get("catalog") or getattr(self.instance, "catalog", None), cleaned.get("title")
        identifiers = cleaned.get("identifiers") or {}
        conditions = [Q(title=title)] if title else []
        for key in ("isbn", "doi"):
            if identifiers.get(key):
                conditions.append(Q(**{f"identifiers__{key}": identifiers[key]}))
        if catalog is not None and conditions:
            clash = Entry.objects.exclude(pk=self.instance.pk).filter(catalog=catalog).filter(reduce(or_, conditions))
            if clash.exists():
                raise forms.ValidationError(
                    _("The catalog already has a publication with this title, ISBN or DOI: %(entry)s")
                    % {"entry": clash.first()}
                )
        upload = cleaned.get("cover_upload")
        if upload is not None and getattr(upload, "content_type", None) not in IMAGE_MIMES:
            self.add_error("cover_upload", _("Not a supported image type."))
        return cleaned


@register(Entry, module="library")
class EntryAdmin(ConsoleModelAdmin):
    """Full CRUD. Authors are the inline rows (ordered); files are added on the Acquisition page."""

    editable = True
    deletable = True
    gallery = True
    form = EntryForm
    autocomplete_fields = ("catalog", "language")
    filter_horizontal = ("categories",)
    raw_id_fields = ("related_entries",)
    list_display = (
        "title",
        "catalog",
        "language",
        "published_at",
        "popularity",
        "rating",
        "withdrawn_at",
        "created_at",
    )
    # Only languages some entry has (the table holds every ISO 639 language).
    list_filter = ("catalog", ("language", admin.RelatedOnlyFieldListFilter), "origin")
    search_fields = ("title", "publisher", "search_authors")
    list_select_related = ("catalog", "language")
    inlines = (EntryAuthorInline, AcquisitionInline)
    show_facets = admin.ShowFacets.ALLOW
    fieldsets = (
        (None, {"fields": ("title", "catalog", "language", "origin", "edition", "publisher", "published_at")}),
        (_("Description"), {"fields": ("summary", "citation", "identifiers", "categories", "related_entries")}),
        (_("Rights and access"), {"fields": ("rights", "config", "withdrawn_at")}),
        (_("Cover"), {"fields": ("cover", "cover_upload")}),
        (
            _("Statistics and search"),
            {
                "classes": ("collapse",),
                "fields": (
                    "popularity",
                    "rating_count",
                    "rating_sum",
                    "page_count",
                    "search_config",
                    "embedding_model",
                    "vector_summary",
                ),
            },
        ),
        # The record's id, creator and timestamps are in the record page's side panel.
    )
    readonly_fields = (
        "cover",
        "id",
        "popularity",
        "rating_count",
        "rating_sum",
        "page_count",
        "search_config",
        "embedding_model",
        "created_at",
        "updated_at",
        "touched_at",
    )

    def record(self, request, obj):
        from apps.core.console_records import entry_record

        return entry_record(super().record(request, obj), request, obj)

    def card(self, request, obj):
        from apps.core.console_records import entry_card

        return entry_card(obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        upload = form.cleaned_data.get("cover_upload")
        if upload is not None:
            from apps.api.services.entry import set_cover

            set_cover(obj, upload, mime=upload.content_type)
            obj.save()

    @admin.display(description=_("Rating"))
    def rating(self, obj):
        if not obj.rating_count:
            return "—"
        return f"{obj.rating_sum / obj.rating_count:.1f} ({obj.rating_count})"

    @admin.display(description=_("Cover"))
    def cover(self, obj):
        if not obj.thumbnail:
            return "—"
        from django.urls import reverse

        # Through the files API (`/data/v1/thumbnails/<id>`): storage paths are not served (S3, no /media/).
        url = reverse("files:thumbnail-download", args=[obj.pk])
        version = obj.thumbnail_hash or ""
        return format_html(
            '<img src="{}?v={}" alt="" style="max-height: 9rem; border: 1px solid var(--e-rule);">', url, version
        )


@register(Author, module="library")
class AuthorAdmin(ConsoleModelAdmin):
    """Renaming an author rebuilds the search text and OAI records of their entries (signals)."""

    editable = True
    deletable = True
    autocomplete_fields = ("catalog",)
    list_display = ("surname", "name", "catalog", "created_at")
    list_filter = ("catalog",)
    search_fields = ("name", "surname")


@register(EntryAuthor, module="library")
class EntryAuthorAdmin(ConsoleModelAdmin):
    editable = True
    deletable = True
    list_display = ("entry", "position", "author")
    search_fields = ("entry__title", "author__surname")
    list_select_related = ("entry", "author")


@register(Category, module="library")
class CategoryAdmin(ConsoleModelAdmin):
    editable = True
    deletable = True
    autocomplete_fields = ("catalog",)
    list_display = ("term", "scheme", "catalog", "created_at")
    list_filter = ("catalog", "scheme")
    search_fields = ("term",)


class FeedForm(forms.ModelForm):
    """`(catalog, url_name)` is a feed's only uniqueness rule, as in `assert_url_name_available` (REST)."""

    class Meta:
        model = Feed
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        catalog, url_name = cleaned.get("catalog"), cleaned.get("url_name")
        if catalog and url_name:
            clash = Feed.objects.filter(catalog=catalog, url_name=url_name).exclude(pk=self.instance.pk)
            if clash.exists():
                suggestion = Feed.available_url_name(catalog, url_name, exclude_pk=self.instance.pk)
                self.add_error("url_name", _("Already used in this catalog. Try '%(s)s'.") % {"s": suggestion})
        return cleaned


@register(Feed, module="library")
class FeedAdmin(ConsoleModelAdmin):
    """Full CRUD. `title` is the multilingual JSON (`{"sk": …, "en": …}`); entries are picked by id."""

    editable = True
    deletable = True
    form = FeedForm
    autocomplete_fields = ("catalog",)
    raw_id_fields = ("entries",)
    filter_horizontal = ("parents",)
    list_display = ("url_name", "catalog", "kind", "source", "per_page", "created_at")
    list_filter = ("catalog", "kind", "source")
    search_fields = ("url_name",)


class AcquisitionForm(forms.ModelForm):
    """A new file goes through `attach_acquisition` (REST and MCP do the same): the type check, then the
    LCP, extraction, outline and OCR queues. Existing rows change relation only."""

    upload = forms.FileField(label=_("File"), required=False)

    class Meta:
        model = Acquisition
        fields = ("entry", "relation", "mime", "file_url")

    def clean(self):
        cleaned = super().clean()
        # `_state.adding`, not `pk is None`: `BaseModel` gives a new row its uuid4 at construction.
        if self.instance._state.adding:
            upload = cleaned.get("upload")
            if upload is None and not cleaned.get("file_url"):
                self.add_error("upload", _("Upload a file (or give an external URL)."))
            elif upload is not None:
                from apps.api.services.entry import InvalidAcquisitionFile, check_acquisition_file

                try:
                    check_acquisition_file(upload, cleaned.get("mime"))
                except InvalidAcquisitionFile as error:
                    self.add_error("upload", str(error))
        return cleaned


@register(Acquisition, module="library")
class AcquisitionAdmin(ConsoleModelAdmin):
    """Add = upload a file to an entry; delete removes the stored file too (signal)."""

    editable = True
    deletable = True
    form = AcquisitionForm
    list_display = ("entry", "relation", "mime", "storage_backend", "created_at")
    list_filter = ("relation", "mime", "storage_backend")
    search_fields = ("entry__title",)
    list_select_related = ("entry",)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("entry", "relation", "mime", "upload", "file_url")
        return ("entry", "relation", "mime", "storage_backend", "content", "file_url", "checksum_cached")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly += ["entry", "mime", "storage_backend", "content", "file_url", "checksum_cached"]
        return readonly

    def save_model(self, request, obj, form, change):
        upload = form.cleaned_data.get("upload")
        if change or upload is None:
            super().save_model(request, obj, form, change)
            return
        from apps.api.services.entry import attach_acquisition

        stored = attach_acquisition(obj.entry, upload, mime=obj.mime, relation=obj.relation)
        # The admin's log entry and redirect need the saved row.
        obj.pk = stored.pk
        obj._state.adding = False


@register(Price, module="library")
class PriceAdmin(ConsoleModelAdmin):
    editable = True
    deletable = True
    list_display = ("acquisition", "value", "currency", "created_at")


@register(EntryReview, module="library")
class EntryReviewAdmin(ConsoleModelAdmin):
    """IP-023. Moderation goes through `ReviewService` so the entry's rating follows."""

    editable = True
    deletable = True
    list_display = ("entry", "user", "rating", "state", "created_at")
    list_filter = ("state", "rating", "entry__catalog__title")
    search_fields = ("entry__title", "user__username", "user__surname", "title", "body")
    raw_id_fields = ("entry", "user", "moderated_by")
    readonly_fields = ("entry", "user", "rating", "moderated_by", "moderated_at")
    actions = ("hide_reviews", "publish_reviews")
    show_facets = admin.ShowFacets.ALWAYS

    def has_add_permission(self, request):
        return False

    def _moderate(self, request, queryset, state):
        for review in queryset.select_related("entry"):
            ReviewService.moderate(review, request.user, state=state)

    @admin.action(description=_("Hide selected reviews"))
    def hide_reviews(self, request, queryset):
        self._moderate(request, queryset, EntryReview.State.HIDDEN)

    @admin.action(description=_("Publish selected reviews"))
    def publish_reviews(self, request, queryset):
        self._moderate(request, queryset, EntryReview.State.PUBLISHED)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        ReviewService.recompute([obj.entry_id])

    # Deletes (single or bulk) recompute through the `post_delete` receiver in `apps.core.services.reviews`.


# --- People & access -----------------------------------------------------------------------------------


class ConsoleUserCreationForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "name", "surname", "auth_source")


class ConsoleUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@register(User, module="accounts")
class UserAdmin(ConsoleMixin, BaseUserAdmin):
    """Local accounts: create, edit, reset passwords (hashed through Django's forms), delete.

    Deleting cascades to the user's loans, licences and history (Django's confirmation lists them);
    deactivating keeps them. Nobody deletes their own account here.
    """

    editable = True
    deletable = True
    form = ConsoleUserChangeForm
    add_form = ConsoleUserCreationForm
    list_display = ("username", "name", "surname", "auth_source", "is_active", "is_superuser", "last_login")
    list_filter = ("is_active", "is_superuser", "auth_source", "password_change_required")
    search_fields = ("username", "name", "surname")
    ordering = ("username",)
    list_per_page = 50
    readonly_fields = ("id", "last_login", "created_at", "updated_at")
    filter_horizontal = ("groups", "user_permissions")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("Person"), {"fields": ("name", "surname", "auth_source")}),
        (
            _("Access"),
            {"fields": ("is_active", "is_superuser", "password_change_required", "groups", "user_permissions")},
        ),
        # The passphrase hash is a credential for LCP licences: never shown (hint only).
        (_("Readium LCP"), {"classes": ("collapse",), "fields": ("lcp_passphrase_hint",)}),
        # Id, last login and timestamps: the record page's side panel and figures.
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "name", "surname", "auth_source", "password1", "password2"),
            },
        ),
    )
    actions = ("deactivate",)
    gallery = True

    def record(self, request, obj):
        from apps.core.console_records import user_record

        return user_record(super().record(request, obj), request, obj)

    def card(self, request, obj):
        from apps.core.console_records import user_card

        return user_card(obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.pk == request.user.pk:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset.exclude(pk=request.user.pk))

    @admin.action(description=_("Deactivate selected users"))
    def deactivate(self, request, queryset):
        from apps.files import revocation

        targets = queryset.exclude(pk=request.user.pk)
        user_ids = list(targets.values_list("pk", flat=True))
        updated = targets.update(is_active=False)
        # `update()` sends no signal: end the users' database-free reading sessions explicitly (IP-036).
        for user_id in user_ids:
            revocation.revoke(revocation.USER, user_id)
        self.message_user(request, _("%(count)s users deactivated.") % {"count": updated})


# `django.contrib.auth` registered Group with the stock admin (it is imported earlier in INSTALLED_APPS).
if admin.site.is_registered(Group):
    admin.site.unregister(Group)


@register(Group, module="accounts")
class GroupAdmin(ConsoleMixin, BaseGroupAdmin):
    editable = True
    deletable = True


@register(AuthSource, module="accounts")
class AuthSourceAdmin(ConsoleModelAdmin):
    """Driver and switch only: `content` holds bind credentials and is never shown."""

    list_display = ("name", "driver", "is_active", "created_at")
    list_filter = ("driver", "is_active")
    exclude = ("content",)


@register(ApiKey, module="accounts")
class ApiKeyAdmin(ConsoleModelAdmin):
    """Metadata only (the key itself is a signed JWT, never stored; keys are issued by `POST
    /api/v1/api_keys` or MCP `create_api_key`, which show the token once). Deactivate or delete to revoke;
    delete is what REST does."""

    editable = True
    deletable = True
    list_display = ("name", "user", "processor", "is_active", "last_seen_at", "created_at")
    list_filter = ("is_active", "processor")
    search_fields = ("name", "user__username")
    list_select_related = ("user",)
    fields = ("name", "user", "processor", "is_active", "last_seen_at", "created_at")
    readonly_fields = ("name", "user", "processor", "last_seen_at", "created_at")
    actions = ("revoke",)

    def has_add_permission(self, request):
        return False

    @admin.action(description=_("Revoke selected keys"))
    def revoke(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, _("%(count)s keys revoked.") % {"count": updated})


@register(UserCatalog, module="accounts")
class UserCatalogAdmin(ConsoleModelAdmin):
    """Who may read, write or manage which catalog."""

    editable = True
    deletable = True
    list_display = ("user", "catalog", "mode")
    list_filter = ("mode", "catalog")
    search_fields = ("user__username", "catalog__title")
    list_select_related = ("user", "catalog")


# --- Lending (the user side; LCP objects are in apps.readium.admin) ------------------------------------


@register(UserAcquisition, module="lending")
class UserAcquisitionAdmin(ConsoleModelAdmin):
    """A reader's access to a file. Granting and withdrawing it updates their content grants (signals)."""

    editable = True
    deletable = True
    list_display = ("acquisition", "user", "type", "range", "expire_at", "created_at")
    list_filter = ("type",)
    search_fields = ("user__username", "acquisition__entry__title")
    list_select_related = ("user", "acquisition__entry")


# --- Reading activity (sensitive, off by default) -----------------------------------------------------


@register(ShelfRecord, module="reading")
class ShelfRecordAdmin(ConsoleModelAdmin):
    list_display = ("user", "entry", "created_at")
    search_fields = ("user__username", "entry__title")
    list_select_related = ("user", "entry")


class AnnotationItemInline(admin.TabularInline):
    model = AnnotationItem
    extra = 0
    can_delete = False
    fields = ("seq", "page", "match", "text", "note")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@register(Annotation, module="reading")
class AnnotationAdmin(ConsoleModelAdmin):
    list_display = ("title", "user_acquisition", "kind", "status", "unresolved_count", "created_at")
    list_filter = ("kind", "status")
    inlines = (AnnotationItemInline,)


@register(AnnotationItem, module="reading")
class AnnotationItemAdmin(ConsoleModelAdmin):
    list_display = ("annotation", "seq", "page", "match", "created_at")
    list_filter = ("match",)


@register(UserActivity, module="reading")
class UserActivityAdmin(ConsoleModelAdmin):
    list_display = ("user", "entry", "action", "count", "last_occurred_at")
    list_filter = ("action",)
    search_fields = ("user__username", "entry__title")
    list_select_related = ("user", "entry")


@register(ReadEvent, module="reading")
class ReadEventAdmin(ConsoleModelAdmin):
    big = True
    list_display = ("entry", "occurred_at", "via", "authenticated")
    list_filter = ("via", "authenticated")


# --- System --------------------------------------------------------------------------------------------


class SettingForm(forms.ModelForm):
    """Writes go through the settings registry, which validates the value against its schema."""

    class Meta:
        model = Setting
        fields = ("key", "value")

    def clean(self):
        cleaned = super().clean()
        key, value = cleaned.get("key"), cleaned.get("value")
        if key:
            try:
                item = settings_store.definition(key)
                if value is not None:
                    item.check(value)
            except settings_store.SettingError as error:
                raise forms.ValidationError(error.message)
        return cleaned


@register(Setting, module="system")
class SettingAdmin(ConsoleModelAdmin):
    """Instance settings (#20). Secret values are never shown; validation is the registry's."""

    editable = True
    deletable = True
    form = SettingForm
    list_display = ("key", "short_value", "updated_by", "updated_at")
    search_fields = ("key",)
    readonly_fields = ("updated_by", "updated_at", "description")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly.append("key")
            if self._secret(obj.key):
                readonly.append("value")
        return readonly

    def get_fields(self, request, obj=None):
        fields = ["key", "description", "value", "updated_by", "updated_at"]
        if obj is not None and self._secret(obj.key):
            fields.remove("value")
        return fields

    @staticmethod
    def _secret(key):
        try:
            return settings_store.definition(key).secret
        except settings_store.SettingError:
            return False

    @admin.display(description=_("Value"))
    def short_value(self, obj):
        if self._secret(obj.key):
            return "••••"
        text = str(obj.value)
        return text if len(text) <= 80 else text[:77] + "…"

    @admin.display(description=_("Description"))
    def description(self, obj):
        try:
            return settings_store.definition(obj.key).description
        except settings_store.SettingError:
            return _("Not a registered key: nothing reads it.")

    def save_model(self, request, obj, form, change):
        settings_store.update(obj.key, obj.value, user=request.user)

    def delete_model(self, request, obj):
        settings_store.update(obj.key, None, user=request.user)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


@register(Language, module="system")
class LanguageAdmin(ConsoleModelAdmin):
    """ISO 639 languages (seeded by `manage.py setup`)."""

    editable = True
    deletable = True
    list_display = ("name", "alpha2", "alpha3")
    search_fields = ("name", "alpha2", "alpha3")


@register(Currency, module="system")
class CurrencyAdmin(ConsoleModelAdmin):
    editable = True
    deletable = True
    list_display = ("name", "code")
    search_fields = ("name", "code")
