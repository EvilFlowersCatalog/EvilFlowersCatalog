import uuid

import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0040_entry_thumbnail_mime"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserActivity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, db_default=django.db.models.functions.datetime.Now()),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("entry_downloaded", "Entry downloaded"),
                            ("license_downloaded", "License downloaded"),
                            ("shelf_added", "Added to shelf"),
                            ("shelf_removed", "Removed from shelf"),
                            ("acquisition_shared", "Acquisition shared"),
                            ("loan_created", "Loan created"),
                            ("loan_renewed", "Loan renewed"),
                            ("loan_returned", "Loan returned"),
                            ("loan_expired", "Loan expired"),
                            ("loan_revoked", "Loan revoked"),
                            ("loan_cancelled", "Loan cancelled"),
                            ("reservation_created", "Reservation created"),
                            ("reservation_available", "Reservation available"),
                            ("reservation_claimed", "Reservation claimed"),
                            ("reservation_expired", "Reservation expired"),
                            ("reservation_cancelled", "Reservation cancelled"),
                        ],
                        max_length=32,
                    ),
                ),
                ("count", models.PositiveIntegerField(default=1)),
                ("last_occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("metadata", models.JSONField(default=dict)),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="core.entry",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User activity",
                "verbose_name_plural": "User activities",
                "db_table": "user_activities",
                "default_permissions": (),
                "indexes": [
                    models.Index(fields=["user", "-last_occurred_at"], name="user_activities_user_last_idx"),
                    models.Index(
                        fields=["user", "entry", "-last_occurred_at"], name="user_activities_entry_last_idx"
                    ),
                    models.Index(fields=["last_occurred_at"], name="user_activities_last_idx"),
                ],
            },
        ),
    ]
