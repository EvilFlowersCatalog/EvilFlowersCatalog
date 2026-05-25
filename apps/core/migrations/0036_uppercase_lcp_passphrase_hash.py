"""IP-008 Phase 3 C4: normalize legacy `User.lcp_passphrase_hash` rows to uppercase.

Older rows may carry lowercase SHA-256 hex from before the `.upper()`
was added at write time. The LCP server compares hex strings case-
sensitively, so any user with a lowercase hash gets rejected when
their reader app fetches a fresh license. This migration upper-cases
every non-null row in one statement.
"""

from django.db import migrations


def _uppercase_existing_hashes(apps, schema_editor):
    User = apps.get_model("core", "User")
    # Only rows that contain at least one lowercase a-f character need updating.
    queryset = User.objects.exclude(lcp_passphrase_hash__isnull=True).exclude(lcp_passphrase_hash="")
    for user in queryset.iterator():
        normalized = user.lcp_passphrase_hash.upper()
        if normalized != user.lcp_passphrase_hash:
            user.lcp_passphrase_hash = normalized
            user.save(update_fields=["lcp_passphrase_hash"])


def _reverse_noop(apps, schema_editor):
    # Uppercasing is one-way; reversing would require knowing the original case.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_merge_20260525_1828"),
    ]

    operations = [
        migrations.RunPython(_uppercase_existing_hashes, _reverse_noop),
    ]
