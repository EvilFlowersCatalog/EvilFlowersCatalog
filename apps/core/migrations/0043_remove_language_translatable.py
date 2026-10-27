from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0042_localized_titles"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="language",
            name="translatable",
        ),
    ]
