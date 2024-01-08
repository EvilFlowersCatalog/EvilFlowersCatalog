# Generated by Django 4.2 on 2023-04-22 21:24

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_removed_safe_delete"),
    ]

    operations = [
        migrations.AddField(
            model_name="entry",
            name="citation",
            field=models.JSONField(null=True),
        ),
        migrations.AddField(
            model_name="entry",
            name="config",
            field=models.JSONField(null=True),
        ),
        migrations.CreateModel(
            name="UserAcquisition",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "type",
                    models.CharField(
                        choices=[("shared", "Shared"), ("personal", "Personal")], default="personal", max_length=10
                    ),
                ),
                ("range", models.CharField(max_length=100, null=True)),
                ("expire_at", models.DateTimeField(null=True)),
                ("acquisition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="core.acquisition")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "User acquisition",
                "verbose_name_plural": "User acquisitions",
                "db_table": "user_acquisitions",
                "default_permissions": (),
            },
        ),
        migrations.CreateModel(
            name="Annotation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("content", models.TextField()),
                (
                    "user_acquisition",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="core.useracquisition"),
                ),
            ],
            options={
                "verbose_name": "Annotation",
                "verbose_name_plural": "Annotations",
                "db_table": "annotations",
                "default_permissions": (),
            },
        ),
    ]
