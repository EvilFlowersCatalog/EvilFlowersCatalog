# Generated by Django 3.2.8 on 2021-10-14 08:38

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_entry_categories'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='language',
            name='native_name',
        ),
    ]