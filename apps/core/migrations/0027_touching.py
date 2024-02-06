# Generated by Django 5.0.1 on 2024-02-01 21:44

from django.db import migrations, models
from django.db.models import Max


def forwards_func(apps, schema_editor):
    Entry = apps.get_model("core", "Entry")
    Catalog = apps.get_model("core", "Catalog")
    Feed = apps.get_model("core", "Feed")
    db_alias = schema_editor.connection.alias

    for entry in Entry.objects.using(db_alias).all():
        entry.touched_at = (
            entry.acquisitions.using(db_alias).aggregate(Max("updated_at")).get("updated_at", entry.updated_at)
        )
        entry.save(using=db_alias)

    for catalog in Catalog.objects.using(db_alias).all():
        catalog.touched_at = (
            catalog.entries.using(db_alias).aggregate(Max("updated_at")).get("updated_at", catalog.updated_at)
        )
        catalog.save(using=db_alias)

    for feed in Feed.objects.using(db_alias).all():
        feed.touched_at = feed.entries.using(db_alias).aggregate(Max("updated_at")).get("updated_at", feed.updated_at)
        feed.save(using=db_alias)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_annotation_items"),
    ]

    operations = [
        migrations.AddField(
            model_name="catalog",
            name="touched_at",
            field=models.DateTimeField(null=True, auto_now=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="touched_at",
            field=models.DateTimeField(null=True, auto_now=True),
        ),
        migrations.AddField(
            model_name="feed",
            name="touched_at",
            field=models.DateTimeField(null=True, auto_now=True),
        ),
        migrations.RunPython(forwards_func),
    ]