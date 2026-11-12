"""
IP-021: one embedding model for entries, paragraphs and queries — multilingual-e5-large-instruct
(1024 dimensions, `halfvec`) from the shared `tei-embed`.

The 768-dimension Arctic vectors cannot be converted, so they are dropped. Runs keep their regions,
paragraph text and sentences; `EVILFLOWERS_EXTRACTION_PIPELINE_VERSION` moves to 2, so
`manage.py backfill_extraction --outdated` re-extracts them with the new model once text-service
embeds through tei-embed.
"""

import pgvector.django
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("extraction", "0003_run_embedding"),
    ]

    operations = [
        migrations.RemoveIndex(model_name="extractionrun", name="extraction_run_embedding"),
        migrations.RemoveIndex(model_name="paragraph", name="extraction_paragraph_embedding"),
        migrations.RemoveField(model_name="extractionrun", name="embedding"),
        migrations.RemoveField(model_name="paragraph", name="embedding"),
        migrations.AddField(
            model_name="extractionrun",
            name="embedding",
            field=pgvector.django.HalfVectorField(dimensions=1024, null=True),
        ),
        migrations.AddField(
            model_name="paragraph",
            name="embedding",
            field=pgvector.django.HalfVectorField(dimensions=1024, null=True),
        ),
        migrations.AddIndex(
            model_name="extractionrun",
            index=pgvector.django.HnswIndex(
                fields=["embedding"], name="extraction_run_embedding", opclasses=["halfvec_cosine_ops"]
            ),
        ),
        migrations.AddIndex(
            model_name="paragraph",
            index=pgvector.django.HnswIndex(
                fields=["embedding"], name="extraction_paragraph_embedding", opclasses=["halfvec_cosine_ops"]
            ),
        ),
    ]
