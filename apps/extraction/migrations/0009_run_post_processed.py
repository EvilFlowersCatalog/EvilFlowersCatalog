from django.db import migrations, models


class Migration(migrations.Migration):
    """#155: every existing run starts unmarked, so the post-processing sweep's first pass is the backfill."""

    dependencies = [
        ("extraction", "0008_section_similarity"),
    ]

    operations = [
        migrations.AddField(
            model_name="extractionrun",
            name="post_processed",
            field=models.PositiveSmallIntegerField(null=True),
        ),
    ]
