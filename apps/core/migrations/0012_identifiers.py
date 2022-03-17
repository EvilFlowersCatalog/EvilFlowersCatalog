# Generated by Django 4.0.2 on 2022-03-17 15:54

import apps.core.validators
import django.contrib.postgres.fields.hstore
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_user_catalogs'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='user',
            options={'default_permissions': ('add', 'delete', 'change', 'view')},
        ),
        migrations.AlterField(
            model_name='entry',
            name='identifiers',
            field=django.contrib.postgres.fields.hstore.HStoreField(null=True, validators=[apps.core.validators.AvailableKeysValidator(keys=['isbn', 'google'])]),
        ),
    ]