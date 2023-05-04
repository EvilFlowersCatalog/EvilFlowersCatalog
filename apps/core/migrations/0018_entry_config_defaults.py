# Generated by Django 4.2.1 on 2023-05-04 18:32

import apps.core.models.entry
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_user_acquisitions'),
    ]

    operations = [
        migrations.AlterField(
            model_name='entry',
            name='config',
            field=models.JSONField(default=apps.core.models.entry.default_entry_config),
        ),
    ]