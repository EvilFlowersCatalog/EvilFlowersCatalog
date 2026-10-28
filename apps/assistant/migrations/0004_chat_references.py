import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_useractivity"),
        ("assistant", "0003_chatmessage_entry"),
    ]

    operations = [
        migrations.AddField(
            model_name="chat",
            name="user_acquisition",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assistant_chats",
                to="core.useracquisition",
            ),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="contexts",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="page",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="pages_read",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
