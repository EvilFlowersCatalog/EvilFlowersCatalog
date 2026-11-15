"""
#171: run-independent addresses. `extraction_locator` keeps one row per key and entry, never deleted with a run;
paragraphs and regions carry their key. Keys of the runs active before this migration are filled in by the
post-processing sweep (`POST_PROCESS_VERSION` 2).
"""

import django.db.models.deletion
import django.db.models.functions.datetime
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0050_entry_cover_hashes"),
        ("extraction", "0010_region_text_search"),
    ]

    operations = [
        migrations.AddField(
            model_name="paragraph",
            name="locator_key",
            field=models.CharField(db_index=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="region",
            name="locator_key",
            field=models.CharField(db_index=True, max_length=40, null=True),
        ),
        migrations.CreateModel(
            name="Locator",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.datetime.Now()),
                ),
                ("key", models.CharField(max_length=40)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("paragraph", "paragraph"),
                            ("section", "section"),
                            ("figure", "figure"),
                            ("table", "table"),
                            ("equation", "equation"),
                        ],
                        max_length=10,
                    ),
                ),
                ("source_page", models.PositiveIntegerField()),
                ("quote_exact", models.TextField(blank=True, default="")),
                ("prefix", models.TextField(blank=True, default="")),
                ("suffix", models.TextField(blank=True, default="")),
                ("first_run", models.UUIDField(null=True)),
                ("last_run", models.UUIDField(null=True)),
                ("detached_at", models.DateTimeField(null=True)),
                (
                    "entry",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="core.entry"),
                ),
            ],
            options={
                "verbose_name": "Locator",
                "verbose_name_plural": "Locators",
                "db_table": "extraction_locator",
                "default_permissions": (),
                "constraints": [models.UniqueConstraint(fields=("entry", "key"), name="extraction_locator_key")],
            },
        ),
    ]
