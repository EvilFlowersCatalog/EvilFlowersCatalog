import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_useractivity"),
        ("assistant", "0002_visible_messages"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assistant_messages",
                to="core.entry",
            ),
        ),
    ]
