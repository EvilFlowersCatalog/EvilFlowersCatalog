import pgvector.django
from django.db import migrations
from pgvector.django import HnswIndex


class Migration(migrations.Migration):
    dependencies = [("extraction", "0002_region_printed_caption_description")]

    operations = [
        migrations.AddField(
            model_name="extractionrun",
            name="embedding",
            field=pgvector.django.VectorField(dimensions=768, null=True),
        ),
        migrations.AddIndex(
            model_name="extractionrun",
            index=HnswIndex(name="extraction_run_embedding", fields=["embedding"], opclasses=["vector_cosine_ops"]),
        ),
        # Publication vectors of the runs already active.
        migrations.RunSQL(
            "UPDATE extraction_run r SET embedding = ("
            "SELECT AVG(p.embedding) FROM extraction_paragraph p WHERE p.run_id = r.id AND p.embedding IS NOT NULL"
            ") WHERE r.is_active",
            migrations.RunSQL.noop,
        ),
    ]
