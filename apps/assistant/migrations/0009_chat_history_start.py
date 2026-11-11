from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0008_chapter_summaries"),
    ]

    operations = [
        migrations.AddField(
            model_name="chat",
            name="history_start",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
