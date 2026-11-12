from django.db import migrations, models


def split_captions(apps, schema_editor):
    """Runs stored before Phase 8: with captions on, `caption` was the model's text."""
    Region = apps.get_model("extraction", "Region")
    batch = []
    for region in Region.objects.exclude(caption__isnull=True).iterator(chunk_size=500):
        provenance = region.provenance or {}
        if provenance.get("caption_model"):
            region.description = region.caption
            region.printed_caption = provenance.get("printed_caption") or None
            region.caption = region.printed_caption or region.description
        else:
            region.printed_caption = region.caption
        batch.append(region)
        if len(batch) >= 500:
            Region.objects.bulk_update(batch, ["caption", "printed_caption", "description"])
            batch = []
    if batch:
        Region.objects.bulk_update(batch, ["caption", "printed_caption", "description"])


class Migration(migrations.Migration):
    dependencies = [("extraction", "0001_initial")]

    operations = [
        migrations.AddField(model_name="region", name="printed_caption", field=models.TextField(null=True)),
        migrations.AddField(model_name="region", name="description", field=models.TextField(null=True)),
        migrations.RunPython(split_captions, migrations.RunPython.noop),
    ]
