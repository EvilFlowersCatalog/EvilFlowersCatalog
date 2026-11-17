"""IP-035: content access (#171–#176) in the console, read-only.

Policies are versioned: the console saves new versions through `apps.content.policies` (the REST/MCP
path); grants follow loans and stay read-only. The disclosure log is in the sensitive Disclosure
audit module (off by default).
"""

from django import forms

from apps.console.options import ConsoleModelAdmin, register
from apps.content.models import ContentDisclosure, ContentGrant, ContentPack, ContentPolicy


class ContentPolicyForm(forms.ModelForm):
    """The document is validated by `apps.content.policies.validate` (the same check as REST and MCP)."""

    class Meta:
        model = ContentPolicy
        fields = ("key", "document")

    def clean(self):
        from apps.content import policies

        cleaned = super().clean()
        if cleaned.get("key") and cleaned.get("document") is not None:
            try:
                policies.validate(cleaned["key"], cleaned["document"])
            except policies.PolicyError as error:
                raise forms.ValidationError(error.message)
        return cleaned


@register(ContentPolicy, module="content")
class ContentPolicyAdmin(ConsoleModelAdmin):
    """Policies are append-only versions: saving a policy stores the next version through
    `policies.save` (validation, row lock, cache drop); older versions stay for the audit trail."""

    editable = True
    form = ContentPolicyForm
    list_display = ("key", "version", "title", "updated_by", "updated_at")
    search_fields = ("key", "title")
    ordering = ("key", "-version")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        return readonly + (["key"] if obj is not None else [])

    def save_model(self, request, obj, form, change):
        from apps.content import policies

        policy, _changed = policies.save(obj.key, obj.document, user=request.user, create=not change)
        stored = ContentPolicy.objects.get(key=policy.key, version=policy.version)
        obj.pk, obj.version, obj.title = stored.pk, stored.version, stored.title
        obj._state.adding = False


@register(ContentGrant, module="content")
class ContentGrantAdmin(ConsoleModelAdmin):
    list_display = ("user", "entry", "source", "source_state", "pages", "granted_at", "revoked_at")
    list_filter = ("source",)
    search_fields = ("user__username", "entry__title")
    list_select_related = ("user", "entry")


@register(ContentPack, module="content")
class ContentPackAdmin(ConsoleModelAdmin):
    list_display = ("key", "user", "query", "created_at")
    search_fields = ("key",)


@register(ContentDisclosure, module="audit")
class ContentDisclosureAdmin(ConsoleModelAdmin):
    list_display = ("created_at", "user", "processor", "surface", "entry", "tier", "basis", "chars", "chars_charged")
    list_filter = ("surface", "tier", "basis", "processor")
    search_fields = ("user__username", "entry__title", "locator")
    list_select_related = ("user", "entry")
    date_hierarchy = "created_at"
