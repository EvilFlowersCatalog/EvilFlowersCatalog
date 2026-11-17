"""IP-035: harvesting (IP-034) in the console (Interoperability module).

Sources have full CRUD with the REST rules (`HarvestSourceForm`, `/api/v1/harvest/sources`): a registered
adapter, its config checked by the adapter, a base URL where the adapter needs one, `sync` mode only for a
connector that can write, a slug that never changes, and the OAI exposure cache dropped after every write.
Runs, probes and file uploads stay REST/MCP operations (`run_harvest`). Conflicts are acknowledged in bulk.
"""

from django import forms
from django.contrib import admin
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleModelAdmin, register
from apps.harvest.models import HarvestConflict, HarvestRecord, HarvestSource, MatchSuggestion


def _adapter_choices():
    from apps.harvest.adapters import registry

    return [(name, name) for name in registry.names()]


class HarvestSourceForm(forms.ModelForm):
    adapter = forms.ChoiceField(choices=_adapter_choices)

    class Meta:
        model = HarvestSource
        fields = "__all__"

    def clean(self):
        from apps.harvest.adapters import registry
        from apps.harvest.adapters.registry import config_problems
        from apps.harvest.forms import sync_problem

        cleaned = super().clean()
        if not self.instance._state.adding and cleaned.get("slug") and cleaned["slug"] != self.instance.slug:
            self.add_error("slug", _("The slug cannot change: created entries carry it in `origin`."))
        adapter_type = registry.get(cleaned.get("adapter") or "")
        if adapter_type is not None and adapter_type.needs_base_url and not cleaned.get("base_url"):
            self.add_error("base_url", _("A %(title)s source needs a base URL.") % {"title": adapter_type.title})
        for problem in config_problems(adapter_type, cleaned.get("config") or {}) if adapter_type else ():
            self.add_error("config", problem)
        probe = HarvestSource(adapter=cleaned.get("adapter") or "", mode=cleaned.get("mode") or "")
        problem = sync_problem(probe)
        if problem:
            self.add_error("mode", problem)
        return cleaned


@register(HarvestSource, module="interop")
class HarvestSourceAdmin(ConsoleModelAdmin):
    editable = True
    deletable = True
    form = HarvestSourceForm
    autocomplete_fields = ("target_catalog",)
    list_display = ("name", "slug", "adapter", "mode", "target_catalog", "enabled", "schedule", "last_run_at")
    list_filter = ("adapter", "mode", "enabled")
    search_fields = ("name", "slug", "base_url")
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {"fields": ("name", "slug", "adapter", "target_catalog", "mode", "enabled", "schedule")}),
        (
            _("Source"),
            {"fields": ("base_url", "metadata_prefix", "set_spec", "system_key", "config", "field_policy")},
        ),
        (_("Exposure"), {"fields": ("oai_reexpose",)}),
        (
            _("State"),
            {"classes": ("collapse",), "fields": ("cursor", "last_run_at", "last_report", "upload_path", "creator")},
        ),
    )
    readonly_fields = ("last_run_at", "last_report", "upload_path")

    def record(self, request, obj):
        """A source: where it reads from, into which catalog, and what its last run did."""
        from apps.console.records import Badge, Stat, changelist_url, when

        record = super().record(request, obj)
        record.eyebrow = f"{_('Harvest source')} · {obj.adapter}"
        record.subtitle = obj.base_url or obj.upload_path or ""
        record.badges.append(Badge(_("enabled"), "ok") if obj.enabled else Badge(_("disabled")))
        record.badges.append(Badge(obj.get_mode_display(), "info"))
        if obj.schedule:
            record.badges.append(Badge(_("schedule %(cron)s") % {"cron": obj.schedule}))
        report = obj.last_report or {}
        counts = report.get("counters", report) if isinstance(report, dict) else {}
        records = HarvestRecord.objects.filter(source=obj)
        record.stats = [
            Stat(
                _("Records"),
                records.count(),
                url=changelist_url(HarvestRecord, source__id__exact=obj.pk),
                tone="accent",
            ),
            Stat(_("Last run"), when(obj.last_run_at)),
            Stat(_("Created"), counts.get("created", "—"), _("last run")),
            Stat(_("Matched"), counts.get("matched", "—"), _("last run")),
            Stat(_("Failed"), counts.get("failed", "—"), _("last run")),
            Stat(
                _("Open conflicts"),
                HarvestConflict.objects.filter(source=obj, acknowledged=False).count(),
                url=changelist_url(HarvestConflict, source__id__exact=obj.pk, acknowledged__exact=0),
            ),
        ]
        return record

    def save_model(self, request, obj, form, change):
        from apps.harvest import exposure

        super().save_model(request, obj, form, change)
        exposure.forget()

    def delete_model(self, request, obj):
        from apps.harvest import exposure

        upload_path = obj.upload_path
        super().delete_model(request, obj)
        exposure.forget()
        if upload_path:
            from apps.harvest.views import _delete_file

            transaction.on_commit(lambda: _delete_file(upload_path))

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


@register(HarvestRecord, module="interop")
class HarvestRecordAdmin(ConsoleModelAdmin):
    list_display = ("record_id", "source", "status", "entry", "sync_state", "deleted", "datestamp")
    list_filter = ("status", "sync_state", "deleted", "source")
    search_fields = ("record_id__exact", "entry__title")
    list_select_related = ("source", "entry")


@register(HarvestConflict, module="interop")
class HarvestConflictAdmin(ConsoleModelAdmin):
    list_display = ("entry", "field", "source", "winner", "acknowledged", "created_at")
    list_filter = ("acknowledged", "winner", "source")
    search_fields = ("entry__title", "field")
    list_select_related = ("entry", "source")
    actions = ("acknowledge",)
    attention = ((_("harvest conflicts to acknowledge"), {"acknowledged": False}),)

    @admin.action(description=_("Acknowledge selected conflicts"))
    def acknowledge(self, request, queryset):
        updated = queryset.filter(acknowledged=False).update(
            acknowledged=True, acknowledged_by=request.user, acknowledged_at=timezone.now()
        )
        self.message_user(request, _("%(count)s conflicts acknowledged.") % {"count": updated})


@register(MatchSuggestion, module="interop")
class MatchSuggestionAdmin(ConsoleModelAdmin):
    list_display = ("record", "entry", "p", "status", "decider_model", "decided_by", "created_at")
    list_filter = ("status", "source")
    list_select_related = ("record", "entry")
    attention = ((_("harvest match suggestions to decide"), {"status": MatchSuggestion.Status.PENDING}),)
