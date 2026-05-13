# IP-003 Phase 0 — partial #50 (no ratings/reviews; tracked in #57).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_add_lcp_passphrase_to_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="entry",
            name="page_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="table_of_contents",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="related_entries",
            field=models.ManyToManyField(
                blank=True,
                related_name="related_to",
                to="core.entry",
            ),
        ),
    ]
