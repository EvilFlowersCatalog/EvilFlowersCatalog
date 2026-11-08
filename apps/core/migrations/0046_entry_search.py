"""IP-020: in-database full-text search (search vector, text search configurations, pg_trgm)."""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


def create_search_objects(apps, schema_editor):
    from apps.core.search.ddl import ensure_search_objects

    ensure_search_objects(schema_editor.connection)


def drop_search_objects(apps, schema_editor):
    from apps.core.search.ddl import drop_search_objects as drop

    drop(schema_editor.connection)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0045_feed_title_not_unique"),
    ]

    operations = [
        TrigramExtension(),
        migrations.RunPython(create_search_objects, drop_search_objects),
        migrations.AddField(
            model_name="entry",
            name="search_config",
            field=models.CharField(editable=False, max_length=63, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="search_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
        migrations.AddIndex(
            model_name="entry",
            index=django.contrib.postgres.indexes.GinIndex(fields=["search_vector"], name="entries_search_vector_gin"),
        ),
        # The vectors are built by 0047 (IP-021): the refresh SQL is application code and already
        # writes the columns 0047 adds, so running it here would fail on a fresh database.
    ]
