"""
#159: figure and table search by words, next to the region vectors. A GIN index over the region search document
(`embedding_text`) in `evilflowers_simple`, the expression `regions.search` matches, as 0005 does for paragraphs.
Built concurrently so extraction writes are not blocked meanwhile.
"""

from django.db import migrations

INDEX = "extraction_region_text_search"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("extraction", "0009_run_post_processed"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON extraction_region "
                "USING gin (to_tsvector('evilflowers_simple'::regconfig, embedding_text))"
            ),
            reverse_sql=f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}",
        ),
    ]
