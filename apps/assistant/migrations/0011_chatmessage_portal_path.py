"""#167: the portal page the reader was on when they sent a message."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0010_ip030_messages_section_summaries"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="portal_path",
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]
