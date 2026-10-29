from django.db import migrations, models

#: Title the portal gives the annotation that holds a reader's page bookmarks.
PAGE_BOOKMARKS_TITLE = "__page_bookmarks__"


def mark_bookmarks(apps, schema_editor):
    Annotation = apps.get_model("core", "Annotation")
    Annotation.objects.filter(title=PAGE_BOOKMARKS_TITLE).update(kind="bookmarks")


class Migration(migrations.Migration):
    """IP-018: annotation layers, and agent highlights on annotation items."""

    dependencies = [
        ("core", "0043_remove_language_translatable"),
    ]

    operations = [
        migrations.AddField(
            model_name="annotation",
            name="kind",
            field=models.CharField(
                choices=[
                    ("drawing", "Drawing"),
                    ("highlight", "Highlight"),
                    ("agent", "Agent"),
                    ("bookmarks", "Bookmarks"),
                ],
                default="drawing",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="annotation",
            name="status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("done", "Done"), ("failed", "Failed")],
                default="done",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="annotation",
            name="prompt",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="annotation",
            name="summary",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="annotation",
            name="unresolved_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="annotationitem",
            name="seq",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="annotationitem",
            name="match",
            field=models.CharField(
                blank=True, choices=[("exact", "exact"), ("partial", "partial")], max_length=16, null=True
            ),
        ),
        migrations.AddField(
            model_name="annotationitem",
            name="quads",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="annotationitem",
            name="text",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="annotationitem",
            name="note",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(mark_bookmarks, migrations.RunPython.noop),
    ]
