from django.contrib import admin
from django.utils.translation import gettext as _

from apps.core.models import Entry, EntryReview, Catalog
from apps.core.services.reviews import ReviewService


def get_related_field(name, admin_order_field=None, short_description=None):
    related_names = name.split("__")

    def dynamic_attribute(obj):
        for related_name in related_names:
            obj = getattr(obj, related_name)
        return obj

    dynamic_attribute.admin_order_field = admin_order_field or name
    dynamic_attribute.short_description = short_description or related_names[-1].title().replace("_", " ")
    return dynamic_attribute


class RelatedFieldAdmin(admin.ModelAdmin):
    def __getattr__(self, attr):
        if "__" in attr:
            return get_related_field(attr)

        # not dynamic lookup, default behaviour
        return self.__getattribute__(attr)


class CatalogAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "url_name", "entries", "is_public")
    search_fields = ("title", "url_name")
    show_facets = admin.ShowFacets.ALWAYS

    @admin.display(description=_("Entries"))
    def entries(self, obj):
        return obj.entries.count()


class EntryAdmin(RelatedFieldAdmin):
    list_display = ("catalog__title", "title", "identifiers", "id")
    list_filter = ("catalog__title",)
    autocomplete_fields = ("catalog",)
    search_fields = ("catalog__title",)
    show_facets = admin.ShowFacets.ALWAYS


class EntryReviewAdmin(admin.ModelAdmin):
    """IP-023. Moderation goes through `ReviewService` so the entry's rating follows."""

    list_display = ("entry", "user", "rating", "state", "created_at")
    list_filter = ("state", "rating", "entry__catalog__title")
    search_fields = ("entry__title", "user__username", "user__surname", "title", "body")
    raw_id_fields = ("entry", "user", "moderated_by")
    readonly_fields = ("entry", "user", "rating", "moderated_by", "moderated_at")
    actions = ("hide_reviews", "publish_reviews")
    show_facets = admin.ShowFacets.ALWAYS

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


admin.site.register(Catalog, CatalogAdmin)
admin.site.register(Entry, EntryAdmin)
admin.site.register(EntryReview, EntryReviewAdmin)
