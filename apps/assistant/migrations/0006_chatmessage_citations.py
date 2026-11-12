from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assistant", "0005_chatmessage_annotations")]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="citations",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
