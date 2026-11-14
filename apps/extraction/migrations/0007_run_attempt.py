from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("extraction", "0006_ip030_region_vectors_references"),
    ]

    operations = [
        migrations.AddField(
            model_name="extractionrun",
            name="attempt",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
