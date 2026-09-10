"""
IP-009 Phase 5 E1: add `License.renewal_count` to track per-loan
renewal history. Backfills to `0` for existing rows.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("readium", "0006_alter_license_unique_together_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="license",
            name="renewal_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
