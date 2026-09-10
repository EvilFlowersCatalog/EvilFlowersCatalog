# IP-003 Phase 3 — Reservation queue.

import uuid

import django.db.models.deletion
import django.db.models.functions
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_add_entry_page_count_toc_related"),
        ("readium", "0003_remove_license_content_id_encryptedcontent_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Reservation",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False),
                ),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.Now()),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.Now()),
                ),
                ("position", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("available", "Available"),
                            ("claimed", "Claimed"),
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("requested_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("available_at", models.DateTimeField(blank=True, null=True)),
                ("claim_deadline", models.DateTimeField(blank=True, null=True)),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservations",
                        to="core.entry",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "claimed_license",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="from_reservation",
                        to="readium.license",
                    ),
                ),
            ],
            options={
                "verbose_name": "Reservation",
                "verbose_name_plural": "Reservations",
                "db_table": "reservations",
                "default_permissions": (),
            },
        ),
        migrations.AddIndex(
            model_name="reservation",
            index=models.Index(fields=["entry", "status", "position"], name="reservation_entry_i_eab1ad_idx"),
        ),
        migrations.AddIndex(
            model_name="reservation",
            index=models.Index(fields=["status", "claim_deadline"], name="reservation_status_574ff3_idx"),
        ),
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["queued", "available"])),
                fields=("entry", "user"),
                name="uniq_active_reservation_per_user_entry",
            ),
        ),
    ]
