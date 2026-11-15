"""Content grants (#175) for every licence and own copy that existed before the signals did.

Historical models, so the logic is written out here; `apps.content.grants.backfill()` does the same for a
database whose rows were flushed after migrating. An opened licence (`active`, `returned`, a device registered)
gets `first_opened_at = starts_at`: older rows never recorded the moment.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    ContentGrant = apps.get_model("content", "ContentGrant")
    License = apps.get_model("readium", "License")
    UserAcquisition = apps.get_model("core", "UserAcquisition")

    existing = set(ContentGrant.objects.values_list("source", "source_id"))
    rows = []
    for license in License.objects.all().iterator(chunk_size=500):
        if ("license", license.pk) in existing:
            continue
        opened = license.state in ("active", "returned") or (license.device_count or 0) > 0
        rows.append(
            ContentGrant(
                user_id=license.user_id,
                entry_id=license.entry_id,
                source="license",
                source_id=license.pk,
                source_state=license.state,
                first_opened_at=license.starts_at if opened else None,
                granted_at=license.starts_at or license.created_at,
            )
        )
    for copy in UserAcquisition.objects.select_related("acquisition").iterator(chunk_size=500):
        if ("acquisition", copy.pk) in existing:
            continue
        rows.append(
            ContentGrant(
                user_id=copy.user_id,
                entry_id=copy.acquisition.entry_id,
                source="acquisition",
                source_id=copy.pk,
                source_state=copy.type,
                pages=copy.range,
                granted_at=copy.created_at,
            )
        )
    ContentGrant.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0003_grants_disclosures"),
        ("readium", "0010_statistics_indexes"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
