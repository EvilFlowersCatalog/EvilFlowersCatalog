# Generated by Django 3.2.6 on 2021-08-27 20:20

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_feed_parents"),
    ]

    operations = [
        migrations.AddField(
            model_name="catalog",
            name="is_public",
            field=models.BooleanField(default=False),
        ),
    ]
