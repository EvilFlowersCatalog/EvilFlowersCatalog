"""
#113: passage search by words, next to the e5 vectors. A GIN index over the paragraph text in
`evilflowers_simple` (accents folded, no stemming), which core 0046 creates. Built concurrently: on the STU
library the table holds about a million paragraphs, and a plain build would block extraction writes meanwhile.
"""

from django.db import migrations

INDEX = "extraction_paragraph_text_search"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("extraction", "0004_e5_halfvec"),
        ("core", "0047_entry_hybrid_search"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON extraction_paragraph "
                "USING gin (to_tsvector('evilflowers_simple'::regconfig, text))"
            ),
            reverse_sql=f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}",
        ),
    ]
