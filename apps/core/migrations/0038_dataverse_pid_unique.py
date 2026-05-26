"""IP-008 Phase 4 D3: unique constraint on (catalog, dataverse_pid).

Two concurrent prepublish callbacks for the same global_id used to race
past the `SELECT FOR UPDATE` and create duplicate Entry rows. With the
partial UniqueConstraint below, a second insert raises IntegrityError
and the sync transaction rolls back cleanly.

The constraint is partial (`WHERE identifiers ? 'dataverse_pid'`) so
non-Dataverse entries are unaffected.
"""

from django.db import migrations

_INDEX_NAME = "unique_dataverse_pid_per_catalog"


CREATE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME}
ON entries (catalog_id, (identifiers->'dataverse_pid'))
WHERE identifiers ? 'dataverse_pid';
"""

DROP_INDEX_SQL = f"DROP INDEX IF EXISTS {_INDEX_NAME};"


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_acquisition_restricted_access"),
    ]

    operations = [
        migrations.RunSQL(CREATE_INDEX_SQL, DROP_INDEX_SQL),
    ]
