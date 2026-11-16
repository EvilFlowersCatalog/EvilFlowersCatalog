from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django_api_forms import BooleanField, Form

from apps.core.identifiers import external_systems
from apps.core.models import Catalog
from apps.harvest.adapters import registry as adapters
from apps.harvest.adapters.registry import config_problems
from apps.harvest.models import POLICY_FIELDS, HarvestSource
from apps.harvest.schedule import InvalidSchedule, parse


def system_keys() -> set[str]:
    """Keys a source may link through: configured external systems, plus keys registered connectors own. Never
    a bibliographic key: a record number written to `isbn` would corrupt matching."""
    return {system.key for system in external_systems()} | adapters.system_keys()


class HarvestSourceForm(Form):
    slug = forms.SlugField(max_length=72)
    name = forms.CharField(max_length=255)
    adapter = forms.CharField(max_length=50)
    config = forms.JSONField(required=False)
    base_url = forms.URLField(max_length=1000, required=False, empty_value=None)
    metadata_prefix = forms.CharField(max_length=50, required=False)
    set_spec = forms.CharField(max_length=255, required=False, empty_value=None)
    system_key = forms.CharField(max_length=100)
    target_catalog_id = forms.ModelChoiceField(queryset=Catalog.objects.all())
    mode = forms.ChoiceField(choices=HarvestSource.Mode.choices, required=False)
    field_policy = forms.JSONField(required=False)
    schedule = forms.CharField(max_length=100, required=False)
    enabled = BooleanField(required=False)
    oai_reexpose = BooleanField(required=False)
    #: null starts the next run from the beginning (a full harvest without `full`).
    cursor = forms.CharField(max_length=40, required=False, empty_value=None)

    def clean_adapter(self):
        name = self.cleaned_data["adapter"]
        if adapters.get(name) is None:
            raise ValidationError(
                _("Unknown adapter '%(name)s'. Registered: %(names)s.")
                % {"name": name, "names": ", ".join(adapters.names())},
                "invalid-adapter",
            )
        return name

    def clean_config(self):
        config = self.cleaned_data.get("config")
        if config in (None, ""):
            return {}
        if not isinstance(config, dict):
            raise ValidationError(_("config must be an object."), "invalid")
        return config

    def clean_system_key(self):
        key = self.cleaned_data["system_key"]
        allowed = system_keys()
        if key not in allowed:
            raise ValidationError(
                _("Unknown external system '%(key)s'. Configured: %(allowed)s (EVILFLOWERS_EXTERNAL_SYSTEMS).")
                % {"key": key, "allowed": ", ".join(sorted(allowed)) or "-"},
                "invalid-system-key",
            )
        return key

    def clean_field_policy(self):
        policy = self.cleaned_data.get("field_policy")
        if policy in (None, ""):
            return {}
        if not isinstance(policy, dict):
            raise ValidationError(_("field_policy must be an object."), "invalid")
        unknown = sorted(set(policy) - set(POLICY_FIELDS))
        if unknown:
            raise ValidationError(
                _("Unknown fields %(unknown)s; known: %(known)s.")
                % {"unknown": ", ".join(unknown), "known": ", ".join(POLICY_FIELDS)},
                "invalid",
            )
        wrong = sorted(key for key, value in policy.items() if value not in HarvestSource.Policy.values)
        if wrong:
            raise ValidationError(
                _("Policies are %(values)s (fields %(fields)s).")
                % {"values": ", ".join(HarvestSource.Policy.values), "fields": ", ".join(wrong)},
                "invalid",
            )
        return policy

    def clean_schedule(self):
        schedule = (self.cleaned_data.get("schedule") or "").strip()
        if schedule:
            try:
                parse(schedule)
            except InvalidSchedule as error:
                raise ValidationError(str(error), "invalid")
        return schedule

    def clean(self):
        adapter_type = adapters.get(self.cleaned_data.get("adapter") or "")
        if adapter_type is not None and adapter_type.needs_base_url and not self.cleaned_data.get("base_url"):
            raise ValidationError(_("A %(title)s source needs base_url.") % {"title": adapter_type.title}, "invalid")
        problems = config_problems(adapter_type, self.cleaned_data.get("config") or {})
        if problems:
            raise ValidationError(" ".join(problems), "invalid-config")
        return self.cleaned_data

    def apply(self, source: HarvestSource) -> HarvestSource:
        """Required fields always; optional ones only when sent (a PUT leaves absent ones as they are)."""
        data = self.cleaned_data
        source.slug = data["slug"]
        source.name = data["name"]
        source.adapter = data["adapter"]
        source.system_key = data["system_key"]
        source.target_catalog = data["target_catalog_id"]
        for name in (
            "config",
            "base_url",
            "set_spec",
            "field_policy",
            "schedule",
            "enabled",
            "oai_reexpose",
            "cursor",
        ):
            if name in data and (data[name] is not None or name in ("base_url", "set_spec", "cursor")):
                setattr(source, name, data[name])
        if data.get("metadata_prefix"):
            source.metadata_prefix = data["metadata_prefix"]
        if data.get("mode"):
            source.mode = data["mode"]
        return source


def sync_problem(source: HarvestSource):
    """Mode `sync` needs a connector that can write (IP-034); the message, or None."""
    connector = adapters.get(source.adapter)
    if source.mode == HarvestSource.Mode.SYNC and (connector is None or not connector._overrides("push")):
        return _("Mode sync needs a connector that can write; '%(name)s' is read only.") % {"name": source.adapter}
    return None


__all__ = ["HarvestSourceForm", "sync_problem", "system_keys"]
