import uuid

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("extraction", "0007_run_attempt"),
    ]

    operations = [
        migrations.CreateModel(
            name="SectionSimilarity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.datetime.Now()),
                ),
                ("source_section", models.TextField()),
                ("target_section", models.TextField()),
                ("score", models.FloatField()),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="section_similarities",
                        to="extraction.extractionrun",
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="extraction.extractionrun",
                    ),
                ),
            ],
            options={
                "verbose_name": "Section similarity",
                "verbose_name_plural": "Section similarities",
                "db_table": "extraction_section_similarity",
                "default_permissions": (),
                "indexes": [
                    models.Index(fields=["source"], name="extraction_section_sim_source"),
                    models.Index(fields=["target"], name="extraction_section_sim_target"),
                ],
            },
        ),
    ]
