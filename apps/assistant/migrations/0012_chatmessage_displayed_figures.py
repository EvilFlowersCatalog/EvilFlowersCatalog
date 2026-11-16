"""#185: figure labels the assistant showed as a gallery on the answer that closes the turn."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0011_chatmessage_portal_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="displayed_figures",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
