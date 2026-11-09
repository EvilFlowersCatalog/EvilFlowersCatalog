from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assistant", "0006_chatmessage_citations")]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="prompt_fingerprint",
            field=models.CharField(blank=True, default="", max_length=12),
        ),
    ]
