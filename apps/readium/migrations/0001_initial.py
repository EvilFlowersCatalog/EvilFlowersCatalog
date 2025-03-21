# Generated by Django 5.1.4 on 2024-12-27 10:29

import django.db.models.deletion
import django.db.models.functions.datetime
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("core", "0029_m2n_unique_constrains"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="License",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.datetime.Now()),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("active", "Active"),
                            ("returned", "Returned"),
                            ("expired", "Expired"),
                            ("revoked", "Revoked"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="draft",
                        max_length=15,
                    ),
                ),
                ("starts_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("entry", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="core.entry")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "License",
                "verbose_name_plural": "Licenses",
                "db_table": "licenses",
                "default_permissions": (),
            },
        ),
    ]
