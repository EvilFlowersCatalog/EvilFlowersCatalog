"""The librarians' review queue of subject suggestions (#191) and the typed section pairs (#190)."""

from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from apps.decider import subjects
from apps.decider.models import SectionRelation, SubjectSuggestion


@admin.register(SubjectSuggestion)
class SubjectSuggestionAdmin(admin.ModelAdmin):
    list_display = ("entry", "category", "p", "status", "catalog", "decider_model", "decided_by", "created_at")
    list_filter = ("status", "catalog", "decider_model")
    search_fields = ("entry__title", "category__term")
    raw_id_fields = ("entry", "category", "catalog", "decided_by")
    readonly_fields = ("trail", "p", "decider_model", "decided_by", "decided_at", "created_at", "updated_at")
    ordering = ("-created_at",)
    actions = ("accept", "reject")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("entry", "category", "catalog", "decided_by")

    @admin.action(description=_("Accept: file the entry under the suggested category"))
    def accept(self, request, queryset):
        done = sum(subjects.accept(suggestion, request.user) for suggestion in queryset)
        self.message_user(request, _("%(count)s suggestions filed.") % {"count": done}, messages.SUCCESS)

    @admin.action(description=_("Reject"))
    def reject(self, request, queryset):
        done = sum(subjects.reject(suggestion, request.user) for suggestion in queryset)
        self.message_user(request, _("%(count)s suggestions rejected.") % {"count": done}, messages.SUCCESS)


@admin.register(SectionRelation)
class SectionRelationAdmin(admin.ModelAdmin):
    list_display = ("a_key", "b_key", "relation", "p", "band", "decider_model", "updated_at")
    list_filter = ("relation", "band", "decider_model")
    search_fields = ("a_key", "b_key")
    readonly_fields = ("probabilities", "created_at", "updated_at")
