"""IP-021: BM25 (pg_search) and semantic search (entry vectors) on top of the IP-020 full-text index."""

import pgvector.django
from django.db import migrations, models


def build_search_columns(apps, schema_editor):
    """Lexical index and the denormalised text (also what IP-020's 0046 used to build)."""
    from apps.core.search.ddl import ensure_search_objects
    from apps.core.search.index import refresh_search_vectors

    ensure_search_objects(schema_editor.connection)
    refresh_search_vectors(connection=schema_editor.connection)


def create_bm25(apps, schema_editor):
    from apps.core.search.ddl import ensure_bm25

    ensure_bm25(schema_editor.connection)


def drop_bm25(apps, schema_editor):
    from apps.core.search.ddl import drop_bm25 as drop

    drop(schema_editor.connection)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0046_entry_search"),
    ]

    operations = [
        pgvector.django.VectorExtension(),
        migrations.AddField(
            model_name="entry",
            name="search_authors",
            field=models.TextField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="search_subjects",
            field=models.TextField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="search_summary",
            field=models.TextField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="embedding",
            field=pgvector.django.HalfVectorField(dimensions=1024, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="embedding_model",
            field=models.CharField(editable=False, max_length=255, null=True),
        ),
        migrations.AddIndex(
            model_name="entry",
            index=pgvector.django.HnswIndex(
                fields=["embedding"], name="entries_embedding_hnsw", opclasses=["halfvec_cosine_ops"]
            ),
        ),
        migrations.RunPython(build_search_columns, migrations.RunPython.noop),
        # After the text columns are filled: pg_search builds the index from the rows present.
        migrations.RunPython(create_bm25, drop_bm25),
    ]
