from django.db import migrations


class Migration(migrations.Migration):
    """Feeds are identified by `(catalog, url_name)`; the title is only a label and may repeat."""

    dependencies = [
        ("core", "0044_annotation_layers"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="feed",
            unique_together={("catalog", "url_name")},
        ),
    ]
