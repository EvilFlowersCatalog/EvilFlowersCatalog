"""The five content-policy presets of #174, version 1 each.

Frozen here as literals: a later change to `apps.content.policies.PRESETS` is a new
version written by a superuser (or a later migration), never a silent rewrite of
what this migration created. Re-running it skips keys that already exist.
"""

import json

from django.db import migrations

PRESETS = json.loads(r"""
{
    "parity": {
        "key": "parity",
        "title": "Today's rules",
        "description": "Open-access text for anyone who sees the entry, inside its IP ranges; Readium LCP text never reaches a model, loan or not. The instance default and the built-in fallback.",
        "tiers": {"open": "full", "loan": "full", "past_loan": "metadata", "none": "metadata"},
        "caps": {
            "outside_ip": "metadata",
            "processor": {"local": "full", "external": "full"},
            "tdm_reserved": "full",
            "generated": "full"
        },
        "grants": {
            "license_states": [],
            "expired_if_opened": false,
            "acquisition_types": ["personal"],
            "survive_hidden": false,
            "member_counts_as_inside_ip": false
        },
        "budget": null,
        "log": {"retention_days": 365}
    },
    "open-access": {
        "key": "open-access",
        "title": "Open access (CC, public domain)",
        "description": "Everything, to everyone who sees the entry, wherever they are and whatever model they use.",
        "tiers": {"open": "full", "loan": "full", "past_loan": "full", "none": "full"},
        "caps": {
            "outside_ip": "full",
            "processor": {"local": "full", "external": "full"},
            "tdm_reserved": "snippet",
            "generated": "full"
        },
        "grants": {
            "license_states": ["ready", "active", "returned", "expired"],
            "expired_if_opened": false,
            "acquisition_types": ["personal", "shared"],
            "survive_hidden": false,
            "member_counts_as_inside_ip": true
        },
        "budget": null,
        "log": {"retention_days": 365}
    },
    "licensed-standard": {
        "key": "licensed-standard",
        "title": "Licensed titles, borrowers keep passages",
        "description": "For licence terms that allow AI reading by borrowers: a live loan reads everything, a past loan passages within a budget.",
        "tiers": {"open": "full", "loan": "full", "past_loan": "passage", "none": "metadata"},
        "caps": {
            "outside_ip": "outline",
            "processor": {"local": "full", "external": "snippet"},
            "tdm_reserved": "snippet",
            "generated": "passage"
        },
        "grants": {
            "license_states": ["active", "returned"],
            "expired_if_opened": true,
            "acquisition_types": ["personal"],
            "survive_hidden": false,
            "member_counts_as_inside_ip": true
        },
        "budget": {"bases": ["past_loan"], "percent_of_book": 10, "window_days": 30},
        "log": {"retention_days": 365}
    },
    "licensed-strict": {
        "key": "licensed-strict",
        "title": "Licensed titles, reader app only",
        "description": "For licence terms that forbid AI use beyond the reader app: a live loan gives snippets, nothing to an external model.",
        "tiers": {"open": "full", "loan": "snippet", "past_loan": "metadata", "none": "metadata"},
        "caps": {
            "outside_ip": "outline",
            "processor": {"local": "full", "external": "metadata"},
            "tdm_reserved": "snippet",
            "generated": "passage"
        },
        "grants": {
            "license_states": ["active"],
            "expired_if_opened": false,
            "acquisition_types": ["personal"],
            "survive_hidden": false,
            "member_counts_as_inside_ip": true
        },
        "budget": null,
        "log": {"retention_days": 365}
    },
    "campus-only": {
        "key": "campus-only",
        "title": "Campus only",
        "description": "As licensed-standard, but nothing beyond the catalogue record outside the allowed IP ranges, for members too.",
        "tiers": {"open": "full", "loan": "full", "past_loan": "passage", "none": "metadata"},
        "caps": {
            "outside_ip": "metadata",
            "processor": {"local": "full", "external": "snippet"},
            "tdm_reserved": "snippet",
            "generated": "passage"
        },
        "grants": {
            "license_states": ["active", "returned"],
            "expired_if_opened": true,
            "acquisition_types": ["personal"],
            "survive_hidden": false,
            "member_counts_as_inside_ip": false
        },
        "budget": {"bases": ["past_loan"], "percent_of_book": 10, "window_days": 30},
        "log": {"retention_days": 365}
    }
}
""")


def create_presets(apps, schema_editor):
    ContentPolicy = apps.get_model("content", "ContentPolicy")
    for key, document in PRESETS.items():
        if not ContentPolicy.objects.filter(key=key).exists():
            ContentPolicy.objects.create(key=key, version=1, title=document["title"], document=document)


def remove_presets(apps, schema_editor):
    ContentPolicy = apps.get_model("content", "ContentPolicy")
    ContentPolicy.objects.filter(key__in=list(PRESETS), version=1).delete()


class Migration(migrations.Migration):
    dependencies = [("content", "0001_content_policy")]

    operations = [migrations.RunPython(create_presets, remove_presets)]
