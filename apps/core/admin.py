from django.contrib import admin
from django.utils.translation import gettext as _

from apps.core.models import Entry, Catalog


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


admin.site.register(Catalog, CatalogAdmin)
admin.site.register(Entry, EntryAdmin)
