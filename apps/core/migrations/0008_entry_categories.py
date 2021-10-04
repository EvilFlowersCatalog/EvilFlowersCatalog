# Generated by Django 3.2.7 on 2021-10-01 18:47

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_popularity'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='entry',
            name='category',
        ),
        migrations.AddField(
            model_name='entry',
            name='categories',
            field=models.ManyToManyField(db_table='entry_categories', related_name='entries', to='core.Category', verbose_name='Category'),
        ),
    ]
