from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assistant", "0004_chat_references")]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="annotations",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
