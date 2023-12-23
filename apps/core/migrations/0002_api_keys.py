# Generated by Django 3.2.3 on 2021-06-05 19:54

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="apikey",
            name="platform",
        ),
        migrations.RemoveField(
            model_name="apikey",
            name="secret",
        ),
        migrations.AddField(
            model_name="apikey",
            name="last_seen_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name="apikey",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
