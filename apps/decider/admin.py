"""The librarians' review queue of subject suggestions (#191) and the typed relations the decider writes
(#190, #202–#210), in the console's Knowledge & decisions module (IP-035)."""

from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleModelAdmin, register
from apps.decider import subjects
from apps.decider.models import MentionRelation, SectionRelation, ShowsRelation, SubjectSuggestion


@register(SubjectSuggestion, module="knowledge")
class SubjectSuggestionAdmin(ConsoleModelAdmin):
    list_display = ("entry", "category", "p", "status", "catalog", "decider_model", "decided_by", "created_at")
    list_filter = ("status", "catalog", "decider_model")
    search_fields = ("entry__title", "category__term")
    readonly_fields = ("trail", "p", "decider_model", "decided_by", "decided_at", "created_at", "updated_at")
    ordering = ("-created_at",)
    actions = ("accept", "reject")
    attention = ((_("subject suggestions to review"), {"status": SubjectSuggestion.Status.PENDING}),)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("entry", "category", "catalog", "decided_by")

    # Deciding is the two actions below (they go through `apps.decider.subjects`); the form stays read-only.

    @admin.action(description=_("Accept: file the entry under the suggested category"))
    def accept(self, request, queryset):
        done = sum(subjects.accept(suggestion, request.user) for suggestion in queryset)
        self.message_user(request, _("%(count)s suggestions filed.") % {"count": done}, messages.SUCCESS)

    @admin.action(description=_("Reject"))
    def reject(self, request, queryset):
        done = sum(subjects.reject(suggestion, request.user) for suggestion in queryset)
        self.message_user(request, _("%(count)s suggestions rejected.") % {"count": done}, messages.SUCCESS)


@register(SectionRelation, module="knowledge")
class SectionRelationAdmin(ConsoleModelAdmin):
    list_display = ("a_key", "b_key", "relation", "p", "band", "decider_model", "updated_at")
    list_filter = ("relation", "band", "decider_model")
    search_fields = ("a_key", "b_key")


@register(MentionRelation, module="knowledge")
class MentionRelationAdmin(ConsoleModelAdmin):
    list_display = ("section_key", "concept_key", "relation", "p", "band", "decider_model", "updated_at")
    list_filter = ("relation", "band", "decider_model")
    search_fields = ("section_key", "concept_key")


@register(ShowsRelation, module="knowledge")
class ShowsRelationAdmin(ConsoleModelAdmin):
    list_display = ("paragraph_key", "figure_key", "answer", "p", "band", "decider_model", "updated_at")
    list_filter = ("answer", "band", "decider_model")
    search_fields = ("paragraph_key", "figure_key")
